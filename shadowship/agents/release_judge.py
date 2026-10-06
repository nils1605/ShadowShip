"""Release Judge agent: score the rehearsal, decide PROMOTE or BLOCK, and write the report.

Scoring starts at 100 and subtracts a penalty once per affected step (a step whose tax
and total both changed counts once):
    regression  -25
    suspicious  -10
    slowdown     -5
The verdict is PROMOTE only if the score reaches the threshold AND there are no regressions.

On BLOCK the judge also writes a pytest regression test per regressed journey step (asserting
the baseline behavior) and a rollback plan. On PROMOTE it promotes the release through Agentia.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from shadowship.agents.behavior_diff import MISSING
from shadowship.agents.rehearsal import PLACEHOLDER
from shadowship.config import Config
from shadowship.gitutil import GitError, git, short_sha
from shadowship.integrations import agentia
from shadowship.models import (
    BehaviorDifference,
    Classification,
    ImpactReport,
    Journey,
    JourneyStep,
    JudgeDecision,
    RehearsalResult,
    StepResult,
    Verdict,
)

PENALTIES = {Classification.REGRESSION: 25, Classification.SUSPICIOUS: 10}
SLOWDOWN_PENALTY = 5


# --- Scoring -----------------------------------------------------------------


def _is_slowdown(difference: BehaviorDifference) -> bool:
    return difference.field == "latency_ms"


def score(differences: list[BehaviorDifference]) -> tuple[int, list[str]]:
    """Confidence score 0-100 and a human-readable list of the penalties applied."""
    penalized: dict[tuple, int] = {}
    labels: dict[tuple, str] = {}
    for d in differences:
        if d.classification == Classification.INTENDED:
            continue
        kind = "slowdown" if _is_slowdown(d) else d.classification.value
        key = (d.journey, d.step, kind)
        penalized[key] = SLOWDOWN_PENALTY if _is_slowdown(d) else PENALTIES[d.classification]
        labels[key] = f"-{penalized[key]} {kind}: {d.journey} › {d.step}"
    return max(0, 100 - sum(penalized.values())), list(labels.values())


def decide(score_value: int, differences: list[BehaviorDifference], threshold: int) -> Verdict:
    has_regression = any(d.classification == Classification.REGRESSION for d in differences)
    return Verdict.PROMOTE if score_value >= threshold and not has_regression else Verdict.BLOCK


# --- Summary -------------------------------------------------------------------


def summarize(
    verdict: Verdict, score_value: int, threshold: int, result: RehearsalResult, differences: list[BehaviorDifference]
) -> str:
    """A few plain-language sentences a judge can read in 30 seconds."""
    journeys = len(result.journey_runs_baseline)
    steps = sum(len(run.steps) for run in result.journey_runs_baseline)
    text = f"ShadowShip replayed {journeys} journeys ({steps} requests) against both versions. "

    by_class = Counter(d.classification for d in differences)
    regressions = [d for d in differences if d.classification == Classification.REGRESSION]
    if regressions:
        first = regressions[0]
        affected = len({(d.journey, d.step) for d in regressions})
        text += (
            f"{affected} step{'s' if affected != 1 else ''} in existing journeys now behave differently. "
            f"For example, in '{first.journey}' › '{first.step}', {first.field} changed from "
            f"{first.baseline} to {first.candidate}. "
        )
    elif by_class[Classification.SUSPICIOUS]:
        text += f"{by_class[Classification.SUSPICIOUS]} suspicious differences need a human look. "
    elif by_class[Classification.INTENDED]:
        text += f"Only intended changes were found ({by_class[Classification.INTENDED]}). "
    else:
        text += "Both versions behaved identically. "

    if verdict == Verdict.PROMOTE:
        text += "The change is safe to release."
    else:
        text += f"The release is blocked (confidence {score_value}, threshold {threshold})."
    return text


# --- Generated regression tests ---------------------------------------------------


def _identifier(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_").lower()


def _baseline_step(result: RehearsalResult, journey: str, step: str) -> StepResult | None:
    for run in result.journey_runs_baseline:
        if run.journey == journey:
            return next((s for s in run.steps if s.step == step), None)
    return None


def _python_value(value: Any) -> str:
    """Python source for a journey value; "{name}" placeholders become variable references."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value)
        if whole:
            return whole.group(1)
        if PLACEHOLDER.search(value):
            return "f" + repr(value)
        return repr(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k!r}: {_python_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_python_value(v) for v in value) + "]"
    return repr(value)


def _python_path(dotted: str) -> str:
    """'items.0.price' -> '["items"][0]["price"]'."""
    return "".join(f"[{part}]" if part.isdigit() else f"[{part!r}]" for part in dotted.split("."))


def _request_line(step: JourneyStep) -> str:
    path = _python_value(step.path)
    if step.body is None:
        return f"client.request({step.method!r}, {path})"
    return f"client.request({step.method!r}, {path}, json={_python_value(step.body)})"


def _test_function(
    journey: Journey, step_name: str, differences: list[BehaviorDifference], baseline: StepResult, head: str
) -> str:
    """A self-contained test that replays the journey up to the regressed step and asserts baseline behavior."""
    lines = [
        f"def test_{_identifier(journey.name)}__{_identifier(step_name)}():",
        f'    """{journey.name} › {step_name}: {differences[0].reason}"""',
        "    client = TestClient(app)",
    ]
    for step in journey.steps:
        lines += ["", f"    # {step.name}"]
        if step.name == step_name:
            lines.append(f"    response = {_request_line(step)}")
            break
        if step.capture:
            lines.append(f"    response = {_request_line(step)}")
            for name, source in step.capture.items():
                lines.append(f"    {name} = response.json(){_python_path(source)}")
        else:
            lines.append(f"    {_request_line(step)}")

    lines.append(f"    assert response.status_code == {baseline.status_code!r}")
    body_checks = [d for d in differences if d.field not in ("status_code", "latency_ms") and d.baseline != MISSING]
    if body_checks:
        lines.append("    body = response.json()")
    for d in body_checks:
        target = "body" if d.field == "(body)" else f"body{_python_path(d.field)}"
        lines.append(f"    assert {target} == {d.baseline!r}  # {head} returned {d.candidate!r}")
    return "\n".join(lines)


def generate_tests(
    differences: list[BehaviorDifference],
    result: RehearsalResult,
    journeys: list[Journey],
    config: Config,
    base: str,
    head: str,
    out_dir: Path,
    timestamp: datetime,
) -> list[Path]:
    """Write one test file per journey with regressions; one test per regressed step.

    The tests only need the app and FastAPI's TestClient (no ShadowShip import), so they can be
    copied into any branch. They pass on the base and fail on the candidate until the bug is fixed.
    """
    regressions = [d for d in differences if d.classification == Classification.REGRESSION]
    if not regressions or not config.app.asgi_app:
        return []

    module, _, attr = config.app.asgi_app.partition(":")
    app_import = f"from {module} import {attr}" + ("" if attr == "app" else " as app")
    journeys_by_name = {j.name: j for j in journeys}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "__init__.py").touch()

    written = []
    for journey_name in dict.fromkeys(d.journey for d in regressions):
        journey = journeys_by_name.get(journey_name)
        if journey is None:
            continue
        steps: dict[str, list[BehaviorDifference]] = {}
        for d in regressions:
            if d.journey == journey_name:
                steps.setdefault(d.step, []).append(d)

        functions = []
        for step_name, step_diffs in steps.items():
            baseline = _baseline_step(result, journey_name, step_name)
            if baseline is not None:
                functions.append(_test_function(journey, step_name, step_diffs, baseline, head))
        if not functions:
            continue

        header = f'''"""Regression test generated by ShadowShip on {timestamp:%Y-%m-%d %H:%M}.

Asserts the behavior of {base}, which {head} broke.
"""

from fastapi.testclient import TestClient

{app_import}


'''
        path = out_dir / f"test_regression_{_identifier(journey_name)}.py"
        path.write_text(header + "\n\n\n".join(functions) + "\n")
        written.append(path)
    return written


# --- Rollback plan -------------------------------------------------------------


def rollback_plan(base: str, head: str, repo_root: str | Path = ".") -> str:
    base_sha, head_sha = short_sha(repo_root, base), short_sha(repo_root, head)
    try:
        target = git(repo_root, "describe", "--tags", "--abbrev=0", "--match", "release/*", base)
        target_note = f"the last promoted release `{target}`"
    except GitError:
        target, target_note = base_sha, f"`{base}` at `{base_sha}`"
    commits = git(repo_root, "log", "--oneline", f"{base}..{head}").splitlines()
    commit_lines = "\n".join(f"   - `{c}`" for c in commits) or "   - (none)"

    return f"""The live version stays {target_note}. `{head}` (`{head_sha}`) must not be promoted.

1. Do not merge or deploy `{head}`.
2. If `{head}` was already deployed, redeploy `{target}`: `git checkout {target}` and run your deploy.
3. If `{head}` was already merged, revert its commits with `git revert --no-edit {base_sha}..{head_sha}`:
{commit_lines}
4. Fix the regressions, make the generated tests in `tests/generated/` pass, then rerun
   `shadowship rehearse --base {base} --head {head}`."""


# --- Report -------------------------------------------------------------------------


def _cell(value: object, width: int = 40) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


CLASS_ICON = {
    Classification.INTENDED: "✅ intended",
    Classification.SUSPICIOUS: "⚠️ suspicious",
    Classification.REGRESSION: "❌ regression",
}


def render_report(
    decision: JudgeDecision,
    result: RehearsalResult,
    differences: list[BehaviorDifference],
    config: Config,
    base: str,
    head: str,
    base_sha: str,
    head_sha: str,
    timestamp: datetime,
    impact: ImpactReport | None = None,
) -> str:
    icon = "✅" if decision.verdict == Verdict.PROMOTE else "❌"
    out = [
        "# Release Rehearsal Report",
        "",
        f"**Verdict: {icon} {decision.verdict.value}** · Confidence **{decision.score}/100** "
        f"(threshold {config.judge.promote_threshold})",
        "",
        f"Baseline `{base}` (`{base_sha}`) vs candidate `{head}` (`{head_sha}`) · {timestamp:%Y-%m-%d %H:%M}",
        "",
        "## Summary",
        "",
        decision.summary,
    ]
    if decision.release_tag:
        out += ["", f"Promoted as `{decision.release_tag}`."]

    out += ["", "## Blast radius", ""]
    if impact:
        out += [
            f"**Intent:** {impact.intent_summary}",
            "",
            "**Changed files:** " + ", ".join(f"`{f}`" for f in impact.changed_files),
            "",
            "**Changed code:** " + (", ".join(f"`{s}`" for s in impact.changed_symbols) or "none"),
            "",
            "**Affected routes:** " + (", ".join(f"`{r}`" for r in impact.affected_routes) or "none found"),
        ]
        if impact.dependents:
            out += ["", "**Dependents:** " + ", ".join(f"`{d}`" for d in impact.dependents)]
        if impact.risk_hints:
            out += ["", "**Risk hints:**", "", *[f"- {hint}" for hint in impact.risk_hints]]
    else:
        out += ["Not analyzed."]

    out += ["", "## Behavior differences", ""]
    if differences:
        out += [
            "| Journey | Step | Field | Baseline | Candidate | Classification | Why |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in differences:
            out.append(
                f"| {_cell(d.journey)} | {_cell(d.step)} | `{_cell(d.field)}` | {_cell(d.baseline)} "
                f"| {_cell(d.candidate)} | {CLASS_ICON[d.classification]} | {_cell(d.reason, 200)} |"
            )
    else:
        out += ["None. Baseline and candidate behaved identically."]

    if decision.penalties:
        out += ["", "**Score breakdown:** 100 " + " ".join(p.split(" ", 1)[0] for p in decision.penalties)
                + f" = {decision.score}", ""]
        out += [f"- {p}" for p in decision.penalties]

    relevant = set(impact.relevant_journeys) if impact else set()
    out += ["", "## Journeys replayed", "", "| Journey | Requests | In blast radius | Differences |", "|---|---|---|---|"]
    for run in result.journey_runs_baseline:
        counts = Counter(d.classification for d in differences if d.journey == run.journey)
        found = ", ".join(f"{n} {c.value}" for c, n in counts.items()) or "none"
        flag = "yes" if run.journey in relevant else ("no" if impact else "?")
        out.append(f"| {run.journey} | {len(run.steps)} | {flag} | {found} |")

    if decision.generated_tests:
        out += ["", "## Generated regression tests", ""]
        out += ["These tests assert the baseline behavior. They fail on the candidate until the regression is fixed:", ""]
        out += [f"- `{path}`" for path in decision.generated_tests]
        out += ["", "Run them with `pytest tests/generated/`."]

    if decision.rollback_plan:
        out += ["", "## Rollback plan", "", decision.rollback_plan]

    return "\n".join(out) + "\n"


# --- Orchestration -------------------------------------------------------------


def judge(
    result: RehearsalResult,
    differences: list[BehaviorDifference],
    journeys: list[Journey],
    config: Config,
    base: str,
    head: str,
    repo_root: str | Path = ".",
    impact: ImpactReport | None = None,
    timestamp: datetime | None = None,
) -> JudgeDecision:
    """Score, decide, write tests/rollback plan on BLOCK, promote on PROMOTE, and write the report."""
    repo_root = Path(repo_root)
    timestamp = timestamp or datetime.now()
    threshold = config.judge.promote_threshold

    score_value, penalties = score(differences)
    verdict = decide(score_value, differences, threshold)
    decision = JudgeDecision(
        verdict=verdict,
        score=score_value,
        summary=summarize(verdict, score_value, threshold, result, differences),
        penalties=penalties,
    )

    reports_dir = repo_root / config.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"rehearsal-{timestamp:%Y%m%d-%H%M%S}.md"
    decision.report_path = str(report_path.relative_to(repo_root))

    if verdict == Verdict.BLOCK:
        tests = generate_tests(
            differences, result, journeys, config, base, head, repo_root / config.generated_tests_dir, timestamp
        )
        decision.generated_tests = [str(path.relative_to(repo_root)) for path in tests]
        decision.rollback_plan = rollback_plan(base, head, repo_root)
    else:
        decision.release_tag = agentia.promote_release(head, decision.report_path, repo_root)

    report_path.write_text(
        render_report(
            decision, result, differences, config, base, head,
            short_sha(repo_root, base), short_sha(repo_root, head), timestamp, impact,
        )
    )
    agentia.post_report(report_path)
    return decision
