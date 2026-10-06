"""Behavior Diff agent: compare baseline and candidate runs step by step and classify each difference.

Rule-based classification (first matching rule wins):

  Status changes
    1. Candidate returns 5xx or fails, baseline did not      -> regression
    2. Baseline rejected the request (4xx), candidate accepts -> intended  (new behavior)
    3. Baseline accepted the request, candidate rejects       -> regression
    4. Any other status change                                -> suspicious

  Response body changes (same status)
    5. Both responses are errors with different details       -> suspicious
    6. The journey exercises new behavior (rule 2 fired)      -> suspicious (side effect of the new feature?)
    7. Otherwise: an existing journey that worked on baseline
       now returns different data                             -> regression

  Latency
    8. Candidate slower than baseline by more than the configured
       percentage and minimum milliseconds                    -> suspicious
"""

from __future__ import annotations

import json
import re
from typing import Any

from deepdiff import DeepDiff

from shadowship.agents.impact_scout import route_matches
from shadowship.config import DiffConfig
from shadowship.integrations.llm import LLM
from shadowship.models import BehaviorDifference, Classification, ImpactReport, JourneyRun, RehearsalResult, StepResult

MISSING = "(missing)"


def _ok(status: int | None) -> bool:
    return status is not None and 200 <= status < 300


def _failed(step: StepResult) -> bool:
    """Server error or no usable response at all."""
    return step.error is not None or step.status_code is None or step.status_code >= 500


def body_differences(baseline: Any, candidate: Any, ignore_fields: list[str]) -> list[tuple[str, Any, Any]]:
    """Return (dotted field path, baseline value, candidate value) for every differing JSON value.

    Keys listed in `ignore_fields` are skipped at any depth (ids, timestamps, ...).
    """
    exclude = [rf"\['{re.escape(name)}'\]" for name in ignore_fields]
    diff = DeepDiff(baseline, candidate, exclude_regex_paths=exclude, view="tree")
    changes = []
    for report_type, levels in diff.items():
        for level in levels:
            path = ".".join(str(part) for part in level.path(output_format="list")) or "(body)"
            old = MISSING if "added" in report_type else level.t1
            new = MISSING if "removed" in report_type else level.t2
            changes.append((path, old, new))
    return sorted(changes, key=lambda change: change[0])


def _status_difference(
    journey: str, base: StepResult, cand: StepResult
) -> BehaviorDifference | None:
    if base.status_code == cand.status_code and _failed(base) == _failed(cand):
        return None

    def make(classification: Classification, reason: str) -> BehaviorDifference:
        return BehaviorDifference(
            journey, base.step, "status_code", base.status_code, cand.status_code, classification, reason
        )

    if _failed(cand) and not _failed(base):
        detail = cand.error or f"HTTP {cand.status_code}"
        return make(Classification.REGRESSION, f"Candidate fails where baseline worked ({detail}).")
    if not _ok(base.status_code) and _ok(cand.status_code):
        return make(Classification.INTENDED, "Request rejected by baseline is now accepted: new behavior.")
    if _ok(base.status_code) and not _ok(cand.status_code):
        return make(Classification.REGRESSION, "Request that worked on baseline is now rejected.")
    return make(Classification.SUSPICIOUS, "Status code changed.")


def _route_note(step: StepResult, impact: ImpactReport | None) -> str:
    if impact is None:
        return ""
    route = f"{step.method} {step.path}"
    if any(route_matches(affected, step.method, step.path) for affected in impact.affected_routes):
        return f" {route} is in the blast radius, but this journey only uses existing features."
    return f" {route} is outside the blast radius of the change."


def _latency_difference(journey: str, base: StepResult, cand: StepResult, config: DiffConfig) -> BehaviorDifference | None:
    increase_ms = cand.latency_ms - base.latency_ms
    if base.latency_ms <= 0 or increase_ms < config.latency_min_increase_ms:
        return None
    increase_pct = increase_ms / base.latency_ms * 100
    if increase_pct <= config.latency_threshold_pct:
        return None
    return BehaviorDifference(
        journey, base.step, "latency_ms", base.latency_ms, cand.latency_ms, Classification.SUSPICIOUS,
        f"Candidate is {increase_pct:.0f}% slower (+{increase_ms:.0f} ms).",
    )


def compare_journey(
    baseline: JourneyRun, candidate: JourneyRun, config: DiffConfig, impact: ImpactReport | None = None
) -> list[BehaviorDifference]:
    """Compare one journey's baseline and candidate runs step by step."""
    differences: list[BehaviorDifference] = []
    pairs = list(zip(baseline.steps, candidate.steps))

    status_diffs = [_status_difference(baseline.journey, base, cand) for base, cand in pairs]
    exercises_new_behavior = any(d and d.classification == Classification.INTENDED for d in status_diffs)

    for (base, cand), status_diff in zip(pairs, status_diffs):
        if status_diff:
            # The body of a different status is expected to differ; report the status change only.
            differences.append(status_diff)
            continue

        if not _failed(base):
            for field, old, new in body_differences(base.body, cand.body, config.ignore_fields):
                if not _ok(base.status_code):
                    classification, reason = Classification.SUSPICIOUS, "Error response changed."
                elif exercises_new_behavior:
                    classification = Classification.SUSPICIOUS
                    reason = "Output changed in a journey that uses new behavior; check it is part of the change."
                else:
                    classification = Classification.REGRESSION
                    reason = "Existing journey that worked on baseline now returns different data."
                    reason += _route_note(base, impact)
                differences.append(
                    BehaviorDifference(baseline.journey, base.step, field, old, new, classification, reason)
                )

        latency = _latency_difference(baseline.journey, base, cand, config)
        if latency:
            differences.append(latency)

    return differences


def compare_runs(
    result: RehearsalResult,
    config: DiffConfig,
    impact: ImpactReport | None = None,
    llm: LLM | None = None,
) -> list[BehaviorDifference]:
    """Compare every journey replayed against both baseline and candidate, then (optionally) let the LLM
    refine the rule-based classifications using the change's intent."""
    candidates = {run.journey: run for run in result.journey_runs_candidate}
    differences = []
    for baseline in result.journey_runs_baseline:
        if baseline.journey in candidates:
            differences.extend(compare_journey(baseline, candidates[baseline.journey], config, impact))
    if llm is not None and differences:
        llm_classify(llm, differences, result, impact)
    return differences


# --- LLM classification ----------------------------------------------------------------

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "classification": {"type": "string", "enum": [c.value for c in Classification]},
                    "reason": {"type": "string", "description": "One sentence a reviewer can act on."},
                },
                "required": ["index", "classification", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}


def _locked(difference: BehaviorDifference) -> bool:
    """Server errors and failed requests stay regressions, whatever the LLM thinks."""
    return difference.field == "status_code" and difference.reason.startswith("Candidate fails")


def llm_classify(
    llm: LLM, differences: list[BehaviorDifference], result: RehearsalResult, impact: ImpactReport | None
) -> None:
    """Refine classifications in place. Leaves the rule-based ones untouched if the LLM is unavailable."""
    requests = {
        (run.journey, step.step): f"{step.method} {step.path}" for run in result.journey_runs_baseline for step in run.steps
    }
    rows = [
        {
            "index": i,
            "journey": d.journey,
            "step": d.step,
            "request": requests.get((d.journey, d.step), ""),
            "field": d.field,
            "baseline": d.baseline,
            "candidate": d.candidate,
            "rule_based_guess": d.classification.value,
        }
        for i, d in enumerate(differences)
    ]
    intent = impact.intent_summary if impact else "unknown"
    intended = "\n".join(f"- {change}" for change in impact.intended_changes) if impact else ""
    prompt = (
        "Classify each difference between baseline and candidate responses to the same request.\n"
        "- intended: the change's intent clearly explains this difference.\n"
        "- regression: existing behavior changed in a way the intent does not explain "
        "(e.g. a different total for a request that does not use the new feature).\n"
        "- suspicious: plausibly related to the intent but needs a human to confirm.\n"
        "Prefer regression over intended when unsure whether customers relying on the old behavior are affected.\n\n"
        f"Intent of the change: {intent}\n"
        + (f"Intended behavior changes:\n{intended}\n" if intended else "")
        + f"\n<differences>\n{json.dumps(rows, default=str, indent=1)}\n</differences>"
    )
    answer = llm.ask_json("classification", prompt, CLASSIFY_SCHEMA)
    if not answer:
        return
    for item in answer["classifications"]:
        index = item["index"]
        if not 0 <= index < len(differences) or _locked(differences[index]):
            continue
        difference = differences[index]
        difference.classification = Classification(item["classification"])
        difference.reason = item["reason"]
        difference.classified_by = "llm"
