"""All Agentia Headless calls live here.

Agentia Headless is Copado's DevOps CLI (`npm install -g @copado/agentia-cli`). ShadowShip plugs into it
in two ways:

1. As a local quality gate. `agentia cicd work submit` (and `agentia cicd work test`) run the project's
   `.agentia_quality_gates.sh` before a user story is promoted. Ours runs `shadowship rehearse`, so a
   BLOCK verdict (exit code 1) stops the promotion and a PROMOTE verdict (exit code 0) lets Agentia
   promote it through the Copado pipeline.
2. Through the CLI, when `agentia` is installed and has a CICD API key (`agentia setup`): we read the
   Copado user story being rehearsed and, if configured, write the verdict onto it.

Without the CLI or credentials every function falls back to a local equivalent, so the demo always runs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from shadowship.config import AgentiaConfig
from shadowship.gitutil import git, short_sha

log = logging.getLogger("shadowship.agentia")

# Set by .agentia_quality_gates.sh so we know Agentia is waiting on our exit code.
GATE_ENV_VAR = "SHADOWSHIP_AGENTIA_GATE"
# Copado's limit for long text fields on a user story.
MAX_FIELD_CHARS = 30000


class AgentiaError(RuntimeError):
    pass


def _run(*args: str, timeout: float = 60) -> Any:
    """Run `agentia <args> --json` and return the `result` part of its JSON output."""
    try:
        proc = subprocess.run(["agentia", *args, "--json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentiaError(f"agentia {' '.join(args)}: {exc}") from exc
    try:
        output = json.loads(proc.stdout)
    except json.JSONDecodeError:
        output = None
    if proc.returncode != 0 or not isinstance(output, dict):
        message = (output or {}).get("message") if isinstance(output, dict) else None
        raise AgentiaError(f"agentia {' '.join(args)} failed: {message or proc.stderr.strip() or proc.stdout.strip()}")
    return output.get("result", output)


@lru_cache(maxsize=1)
def cli_ready() -> bool:
    """True when the Agentia CLI is installed and has a Copado CICD API key stored."""
    if shutil.which("agentia") is None:
        return False
    try:
        credentials = _run("auth", "get", timeout=15).get("credentials", [])
    except (AgentiaError, AttributeError):
        return False
    return any(c.get("type") == "cicd" and c.get("set") for c in credentials)


def live(config: AgentiaConfig | None) -> bool:
    return config is not None and config.enabled and cli_ready()


def in_quality_gate() -> bool:
    """True when Agentia launched us from `.agentia_quality_gates.sh`."""
    return os.environ.get(GATE_ENV_VAR) == "1"


def _story_args(config: AgentiaConfig) -> list[str]:
    # No ID means "the active work item" for every `agentia cicd work` command.
    return [config.user_story] if config.user_story else []


def story_label(story: dict[str, Any]) -> str:
    """'US-0042 · Add discount code' from a Copado user story record, whatever its key casing."""
    lowered = {key.lower(): value for key, value in story.items() if isinstance(value, str)}
    name = lowered.get("name") or lowered.get("id", "")
    title = next((value for key, value in lowered.items() if "title" in key), "")
    return f"{name} · {title}" if name and title else name or title


def trigger_pipeline(stage: str, payload: dict[str, Any], config: AgentiaConfig | None = None) -> dict[str, Any]:
    """Tell Agentia a ShadowShip stage started. Live: fetches the Copado user story being rehearsed."""
    if not live(config):
        log.info("[agentia local] trigger_pipeline stage=%s payload=%s", stage, payload)
        return {"stage": stage, "status": "logged"}
    try:
        story = _run("cicd", "work", "get", *_story_args(config))
    except AgentiaError as exc:
        log.warning("Agentia: could not read the user story (%s)", exc)
        return {"stage": stage, "status": "error", "error": str(exc)}
    story = story if isinstance(story, dict) else {}
    return {"stage": stage, "status": "ok", "user_story": story_label(story), "story": story}


def provision_environment(ref: str) -> dict[str, Any]:
    """Get an environment running `ref`.

    Agentia environments are Salesforce orgs, so for a web service the Rehearsal agent uses local git
    worktrees instead. This stays a local equivalent on purpose.
    """
    log.info("[agentia local] provision_environment ref=%s (using local git worktree)", ref)
    return {"ref": ref, "provider": "local-worktree"}


def promote_release(ref: str, report_path: str | Path, repo_root: str | Path = ".") -> str:
    """Mark `ref` as promoted by creating the git tag `release/<sha>`. Returns the tag name.

    The Copado promotion itself is done by Agentia: when we run as its quality gate, a PROMOTE verdict
    exits 0 and `agentia cicd work submit` carries on. We never call `submit` from here, because submit
    runs the quality gate, which would run us again. Promoting the same commit twice reuses the tag.
    """
    tag = f"release/{short_sha(repo_root, ref)}"
    existing = git(repo_root, "tag", "--list", tag)
    if existing:
        log.info("promote_release: %s already exists", tag)
        return tag
    git(repo_root, "tag", "-a", tag, f"{ref}^{{commit}}", "-m", f"Promoted by ShadowShip. Report: {report_path}")
    log.info("promote_release: tagged %s as %s", ref, tag)
    return tag


def post_report(report_path: str | Path, summary: str = "", config: AgentiaConfig | None = None) -> dict[str, Any]:
    """Publish the verdict to Agentia. Live and `report_field` set: writes it onto the Copado user story."""
    if not live(config) or not config.report_field:
        log.info("[agentia local] post_report %s", report_path)
        return {"report": str(report_path), "status": "logged"}
    text = f"{summary}\n\nFull report: {report_path}"[:MAX_FIELD_CHARS]
    try:
        _run("cicd", "work", "update", *_story_args(config), f"--{config.report_field}", text)
    except AgentiaError as exc:
        log.warning("Agentia: could not post the report (%s)", exc)
        return {"report": str(report_path), "status": "error", "error": str(exc)}
    return {"report": str(report_path), "status": "posted", "field": config.report_field}
