"""Rehearsal agent: run baseline and candidate side by side and replay journeys against both.

Steps:
  1. Check out each ref into its own git worktree (.shadowship/baseline, .shadowship/candidate).
  2. Start the app from each worktree on its own port and wait for the health check.
  3. Replay every journey in journeys/*.yaml against both servers.
  4. Always stop the servers and remove the worktrees, even on failure.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import yaml

from shadowship.config import Config
from shadowship.models import BehaviorDifference, Journey, JourneyRun, JourneyStep, RehearsalResult, StepResult

BASELINE = "baseline"
CANDIDATE = "candidate"

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class RehearsalError(RuntimeError):
    pass


# --- Journeys ---------------------------------------------------------------


def load_journeys(journeys_dir: str | Path) -> list[Journey]:
    """Load every journey YAML file in `journeys_dir`, sorted by file name."""
    journeys = []
    for path in sorted(Path(journeys_dir).glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        steps = [
            JourneyStep(
                name=step["name"],
                method=step["method"].upper(),
                path=step["path"],
                body=step.get("body"),
                capture=step.get("capture") or {},
            )
            for step in data.get("steps", [])
        ]
        journeys.append(Journey(name=data.get("name", path.stem), steps=steps, description=data.get("description", "")))
    return journeys


def substitute(value: Any, variables: dict[str, Any]) -> Any:
    """Replace `{name}` placeholders in strings, recursively through dicts and lists.

    A string that is exactly one placeholder (e.g. "{cart_id}") is replaced by the raw
    value, so numbers stay numbers. Unknown placeholders raise KeyError.
    """
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            return variables[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(variables[m.group(1)]), value)
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    return value


def extract(body: Any, dotted_path: str) -> Any:
    """Read a value from a JSON body by dotted path, e.g. "items.0.price"."""
    current = body
    for part in dotted_path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def replay_journey(client: httpx.Client, journey: Journey, target: str) -> JourneyRun:
    """Replay one journey step by step, passing captured variables between steps."""
    run = JourneyRun(journey=journey.name, target=target)
    variables: dict[str, Any] = {}

    for step in journey.steps:
        started = time.perf_counter()
        status, body, error = None, None, None
        path = step.path
        try:
            path = substitute(step.path, variables)
            response = client.request(step.method, path, json=substitute(step.body, variables))
            status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = response.text
            for name, source in step.capture.items():
                variables[name] = extract(body, source)
        except KeyError as exc:
            error = f"missing value {exc}"
        except (IndexError, TypeError, ValueError) as exc:
            error = f"could not capture value: {exc}"
        except httpx.HTTPError as exc:
            error = f"request failed: {exc}"
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        run.steps.append(StepResult(step.name, step.method, path, status, body, latency_ms, error))
    return run


# --- Worktrees and servers ---------------------------------------------------


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RehearsalError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@contextlib.contextmanager
def worktree(repo_root: Path, ref: str, path: Path) -> Iterator[Path]:
    """Check out `ref` into `path` as a detached git worktree, removed on exit."""
    _git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if path.exists():  # left over from a crashed run
        _git(repo_root, "worktree", "remove", "--force", str(path))
    _git(repo_root, "worktree", "prune")
    _git(repo_root, "worktree", "add", "--detach", str(path), ref)
    try:
        yield path
    finally:
        with contextlib.suppress(RehearsalError):
            _git(repo_root, "worktree", "remove", "--force", str(path))


def _is_healthy(url: str) -> bool:
    try:
        return httpx.get(url, timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


def _server_command(template: str, port: int) -> list[str]:
    command = shlex.split(template.format(port=port))
    if command and command[0] in ("python", "python3"):
        command[0] = sys.executable  # use the same interpreter (and venv) ShadowShip runs in
    return command


@contextlib.contextmanager
def server(cwd: Path, config: Config, port: int, log_path: Path) -> Iterator[str]:
    """Start the app from `cwd` on `port`, wait until healthy, and stop it on exit."""
    base_url = f"http://127.0.0.1:{port}"
    health_url = base_url + config.app.health_path
    if _is_healthy(health_url):
        raise RehearsalError(f"Something is already running on port {port}. Stop it and try again.")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as log:
        process = subprocess.Popen(
            _server_command(config.app.start_command, port), cwd=cwd, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + config.app.startup_timeout_seconds
            while not _is_healthy(health_url):
                if process.poll() is not None or time.monotonic() > deadline:
                    reason = "exited" if process.poll() is not None else "did not become healthy in time"
                    tail = "\n".join(log_path.read_text().splitlines()[-15:])
                    raise RehearsalError(f"Server in {cwd} {reason}. Last log lines:\n{tail}")
                time.sleep(0.2)
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


# --- Orchestration -----------------------------------------------------------


def run_rehearsal(
    base: str,
    head: str,
    config: Config,
    repo_root: str | Path = ".",
    on_progress: Callable[[str], None] = lambda message: None,
) -> RehearsalResult:
    """Run baseline (`base`) and candidate (`head`) side by side and replay all journeys."""
    repo_root = Path(repo_root).resolve()
    journeys = load_journeys(repo_root / config.journeys_dir)
    if not journeys:
        raise RehearsalError(f"No journeys found in {config.journeys_dir}/")

    work_dir = repo_root / config.worktrees_dir
    targets = [
        (BASELINE, base, config.app.baseline_port),
        (CANDIDATE, head, config.app.candidate_port),
    ]
    runs: dict[str, list[JourneyRun]] = {}

    with contextlib.ExitStack() as stack:
        urls = {}
        for name, ref, port in targets:
            on_progress(f"Checking out {name} ({ref})")
            path = stack.enter_context(worktree(repo_root, ref, work_dir / name))
            on_progress(f"Starting {name} on port {port}")
            urls[name] = stack.enter_context(server(path, config, port, work_dir / f"{name}.log"))

        for name, _, _ in targets:
            runs[name] = []
            for journey in journeys:
                on_progress(f"Replaying {journey.name} against {name}")
                # A fresh client per journey keeps journeys independent of each other.
                with httpx.Client(base_url=urls[name], timeout=10.0) as client:
                    runs[name].append(replay_journey(client, journey, name))

    return RehearsalResult(journey_runs_baseline=runs[BASELINE], journey_runs_candidate=runs[CANDIDATE])


def save_result(
    result: RehearsalResult,
    reports_dir: str | Path,
    base: str,
    head: str,
    differences: list[BehaviorDifference] | None = None,
) -> Path:
    """Write the raw rehearsal result (and differences, if given) as JSON to reports/rehearsal-<timestamp>.json."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"rehearsal-{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {"base": base, "head": head, **asdict(result)}
    if differences is not None:
        payload["differences"] = [asdict(difference) for difference in differences]
    path.write_text(json.dumps(payload, indent=2))
    return path
