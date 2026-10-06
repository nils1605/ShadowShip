"""LLM wrapper and the LLM steps of each agent, using fakes (no network calls)."""

from types import SimpleNamespace

import anthropic
import pytest

from shadowship.agents.behavior_diff import compare_runs, llm_classify
from shadowship.agents.impact_scout import llm_intent
from shadowship.agents.release_judge import judge
from shadowship.config import Config, LLMConfig
from shadowship.integrations.llm import FALLBACK_BETA, LLM
from shadowship.models import (
    BehaviorDifference,
    Classification,
    ImpactReport,
    JourneyRun,
    RehearsalResult,
    StepResult,
    Verdict,
)

# --- the wrapper ------------------------------------------------------------------


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def fake_client(outcome):
    messages = FakeMessages(outcome)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"], "additionalProperties": False}


def test_ask_json_sends_structured_output_request_with_fallback():
    client, messages = fake_client(response('{"x": 1}'))
    llm = LLM(LLMConfig(model="claude-opus-5-5", effort="low"), client=client)

    assert llm.ask_json("task", "prompt", SCHEMA) == {"x": 1}
    assert llm.used == ["task"]
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["messages"] == [{"role": "user", "content": "prompt"}]


@pytest.mark.parametrize("outcome, note", [
    (response("", stop_reason="refusal"), "task: stopped with refusal"),
    (response("", stop_reason="max_tokens"), "task: stopped with max_tokens"),
    (response("not json"), "task: response was not valid JSON"),
])
def test_ask_json_returns_none_on_bad_responses(outcome, note):
    client, _ = fake_client(outcome)
    llm = LLM(LLMConfig(), client=client)
    assert llm.ask_json("task", "prompt", SCHEMA) is None
    assert llm.notes == [note]
    assert llm.available  # one bad answer does not disable later steps


def test_missing_credentials_disables_llm():
    client, messages = fake_client(TypeError('"Could not resolve authentication method. Expected one of api_key"'))
    llm = LLM(LLMConfig(), client=client)
    assert llm.ask_json("a", "prompt", SCHEMA) is None
    assert not llm.available
    assert llm.ask_json("b", "prompt", SCHEMA) is None
    assert len(messages.calls) == 1
    assert llm.notes == ["no Anthropic credentials (set ANTHROPIC_API_KEY)"]


def test_connection_error_disables_llm():
    client, _ = fake_client(anthropic.APIConnectionError(request=None))
    llm = LLM(LLMConfig(), client=client)
    assert llm.ask_json("a", "prompt", SCHEMA) is None
    assert not llm.available


def test_disabled_in_config():
    llm = LLM(LLMConfig(enabled=False))
    assert not llm.available
    assert llm.ask_json("a", "prompt", SCHEMA) is None
    assert llm.notes == ["LLM disabled in shadowship.yaml"]


def test_real_client_without_credentials_falls_back(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")  # no `ant auth login` profile either
    llm = LLM(LLMConfig())
    assert llm.ask_json("a", "prompt", SCHEMA) is None
    assert not llm.available


# --- agents with a scripted LLM ------------------------------------------------------


class ScriptedLLM:
    """Stands in for LLM: returns a canned answer and records prompts."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def ask_json(self, task, prompt, schema, max_tokens=8000):
        self.prompts.append(prompt)
        return self.answer


def test_llm_intent_sends_commits_and_diff():
    llm = ScriptedLLM({"intent_summary": "Adds SAVE10.", "intended_behavior_changes": ["SAVE10 accepted"]})
    assert llm_intent(llm, ["Add SAVE10"], "diff text", "stat")["intent_summary"] == "Adds SAVE10."
    assert "<commit_messages>\nAdd SAVE10\n</commit_messages>" in llm.prompts[0]
    assert "diff text" in llm.prompts[0]


def test_llm_intent_sends_file_list_for_huge_diffs():
    llm = ScriptedLLM(None)
    llm_intent(llm, ["msg"], "x" * 200_000, "demo_app/main.py | 2 +-")
    assert "too large" in llm.prompts[0] and "demo_app/main.py | 2 +-" in llm.prompts[0]
    assert "x" * 1000 not in llm.prompts[0]


def _diff(field, classification, reason="rule reason"):
    return BehaviorDifference("j", "checkout", field, 1, 2, classification, reason)


RESULT = RehearsalResult(
    [JourneyRun("j", "baseline", [StepResult("checkout", "POST", "/checkout", 200, {}, 1.0)])], []
)
IMPACT = ImpactReport(["a.py"], ["POST /checkout"], "Add SAVE10", intended_changes=["SAVE10 accepted"])


def test_llm_classification_overrides_rules_but_not_server_errors():
    differences = [
        _diff("tax", Classification.REGRESSION),
        _diff("status_code", Classification.REGRESSION, "Candidate fails where baseline worked (HTTP 500)."),
        _diff("total", Classification.SUSPICIOUS),
    ]
    llm = ScriptedLLM({"classifications": [
        {"index": 0, "classification": "intended", "reason": "Tax rate change is intended."},
        {"index": 1, "classification": "intended", "reason": "Should never apply."},
        {"index": 99, "classification": "intended", "reason": "Out of range, ignored."},
    ]})
    llm_classify(llm, differences, RESULT, IMPACT)

    assert (differences[0].classification, differences[0].classified_by) == (Classification.INTENDED, "llm")
    assert differences[0].reason == "Tax rate change is intended."
    assert differences[1].classification == Classification.REGRESSION and differences[1].classified_by == "rules"
    assert differences[2].classification == Classification.SUSPICIOUS  # not mentioned: rules kept
    assert "Intent of the change: Add SAVE10" in llm.prompts[0]
    assert "- SAVE10 accepted" in llm.prompts[0]
    assert '"request": "POST /checkout"' in llm.prompts[0]


def test_llm_classification_failure_keeps_rules():
    differences = [_diff("tax", Classification.REGRESSION)]
    llm_classify(ScriptedLLM(None), differences, RESULT, IMPACT)
    assert differences[0].classification == Classification.REGRESSION
    assert differences[0].classified_by == "rules"


def test_compare_runs_skips_llm_when_nothing_differs():
    llm = ScriptedLLM({"classifications": []})
    same = RehearsalResult(RESULT.journey_runs_baseline, [JourneyRun("j", "candidate", RESULT.journey_runs_baseline[0].steps)])
    assert compare_runs(same, Config().diff, IMPACT, llm) == []
    assert llm.prompts == []


def test_judge_uses_llm_narrative_but_keeps_rule_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr("shadowship.agents.release_judge.short_sha", lambda root, ref: "abc1234")
    monkeypatch.setattr("shadowship.agents.release_judge.rollback_plan", lambda base, head, root: "Roll back.")
    llm = ScriptedLLM({"summary": "The change adds SAVE10 but overcharges tax. Fix before release."})
    differences = [_diff("tax", Classification.REGRESSION)]

    decision = judge(RESULT, differences, [], Config(), "main", "feature", repo_root=tmp_path, impact=IMPACT, llm=llm)

    assert decision.verdict == Verdict.BLOCK and decision.score == 75
    assert decision.summary == "The change adds SAVE10 but overcharges tax. Fix before release."
    assert decision.summary_source == "llm"
    assert "The release verdict is BLOCK with confidence 75/100" in llm.prompts[0]
    report = (tmp_path / decision.report_path).read_text()
    assert "_Summary written by claude-opus-5-5; the verdict and score are rule-based._" in report


def test_judge_falls_back_to_rule_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("shadowship.agents.release_judge.short_sha", lambda root, ref: "abc1234")
    monkeypatch.setattr("shadowship.agents.release_judge.rollback_plan", lambda base, head, root: "Roll back.")
    decision = judge(RESULT, [_diff("tax", Classification.REGRESSION)], [], Config(), "main", "feature",
                     repo_root=tmp_path, llm=ScriptedLLM(None))
    assert decision.summary_source == "rules"
    assert decision.summary.startswith("ShadowShip replayed 1 journey (1 request)")
