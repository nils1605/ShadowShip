"""Dataclasses shared between the agents.

Data flows through the pipeline like this:

    Impact Scout  -> ImpactReport
    Rehearsal     -> RehearsalResult (JourneyRuns of StepResults)
    Behavior Diff -> list[BehaviorDifference]
    Release Judge -> JudgeDecision
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Classification(str, Enum):
    """How serious a behavior difference between baseline and candidate is."""

    INTENDED = "intended"
    SUSPICIOUS = "suspicious"
    REGRESSION = "regression"


class Verdict(str, Enum):
    PROMOTE = "PROMOTE"
    BLOCK = "BLOCK"


@dataclass
class ImpactReport:
    """Blast radius of a diff, produced by the Impact Scout."""

    changed_files: list[str]
    affected_routes: list[str]  # e.g. "POST /checkout"
    intent_summary: str
    risk_hints: list[str] = field(default_factory=list)
    changed_symbols: list[str] = field(default_factory=list)  # e.g. "demo_app/main.py::checkout"
    dependents: list[str] = field(default_factory=list)  # files importing a changed module
    relevant_journeys: list[str] = field(default_factory=list)  # journeys that hit an affected route


@dataclass
class JourneyStep:
    """One request in a user journey.

    `capture` maps a variable name to a dotted path in the JSON response
    (e.g. {"cart_id": "id"}). Captured values can be used in later steps
    as `{cart_id}` inside the path or body.
    """

    name: str
    method: str
    path: str
    body: Any = None
    capture: dict[str, str] = field(default_factory=dict)


@dataclass
class Journey:
    """An ordered list of requests that simulates a realistic user flow."""

    name: str
    steps: list[JourneyStep]
    description: str = ""


@dataclass
class StepResult:
    """What happened when one journey step was replayed against one server."""

    step: str
    method: str
    path: str
    status_code: int | None
    body: Any
    latency_ms: float
    error: str | None = None


@dataclass
class JourneyRun:
    """All step results for one journey replayed against one target."""

    journey: str
    target: str  # "baseline" or "candidate"
    steps: list[StepResult] = field(default_factory=list)


@dataclass
class RehearsalResult:
    journey_runs_baseline: list[JourneyRun]
    journey_runs_candidate: list[JourneyRun]


@dataclass
class BehaviorDifference:
    """A single difference between baseline and candidate for one journey step."""

    journey: str
    step: str
    field: str
    baseline: Any
    candidate: Any
    classification: Classification
    reason: str


@dataclass
class JudgeDecision:
    """Final verdict of the Release Judge."""

    verdict: Verdict
    score: int
    summary: str
    penalties: list[str] = field(default_factory=list)
    report_path: str | None = None
    generated_tests: list[str] = field(default_factory=list)
    rollback_plan: str | None = None
    release_tag: str | None = None
