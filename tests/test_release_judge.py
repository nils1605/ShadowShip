import subprocess
from datetime import datetime

import pytest

from shadowship.agents.release_judge import (
    _python_path,
    _python_value,
    decide,
    generate_tests,
    judge,
    rollback_plan,
    score,
    summarize,
)
from shadowship.config import Config
from shadowship.integrations import agentia
from shadowship.models import (
    BehaviorDifference,
    Classification,
    Journey,
    JourneyRun,
    JourneyStep,
    RehearsalResult,
    StepResult,
    Verdict,
)

R, S, I = Classification.REGRESSION, Classification.SUSPICIOUS, Classification.INTENDED
NOW = datetime(2026, 10, 6, 12, 0, 0)


def diff(classification, journey="j", step="checkout", field="tax", baseline=1.0, candidate=2.0):
    return BehaviorDifference(journey, step, field, baseline, candidate, classification, "reason")


# --- scoring and verdict ------------------------------------------------------------


def test_score_penalizes_once_per_step_and_ignores_intended():
    value, penalties = score([diff(R, field="tax"), diff(R, field="total"), diff(I, step="other")])
    assert value == 75
    assert penalties == ["-25 regression: j › checkout"]


def test_score_weights_and_floor():
    assert score([diff(S)])[0] == 90
    assert score([diff(S, field="latency_ms")])[0] == 95
    assert score([diff(R, step=str(n)) for n in range(10)])[0] == 0


def test_any_regression_blocks_even_with_high_score():
    assert decide(95, [diff(R)], threshold=80) == Verdict.BLOCK


def test_low_score_blocks_and_clean_run_promotes():
    assert decide(70, [diff(S)], threshold=80) == Verdict.BLOCK
    assert decide(90, [diff(S)], threshold=80) == Verdict.PROMOTE
    assert decide(100, [], threshold=80) == Verdict.PROMOTE


def test_summary_names_an_example_regression():
    result = RehearsalResult([JourneyRun("j", "baseline", [StepResult("s", "GET", "/", 200, {}, 1.0)])], [])
    text = summarize(Verdict.BLOCK, 75, 80, result, [diff(R, baseline=8.32, candidate=8.72)])
    assert "tax changed from 8.32 to 8.72" in text
    assert "blocked" in text
    assert "safe to release" in summarize(Verdict.PROMOTE, 100, 80, result, [diff(I)])


# --- generated test source --------------------------------------------------------------


def test_python_value_turns_placeholders_into_variables():
    assert _python_value("{cart_id}") == "cart_id"
    assert _python_value("/cart/{cart_id}/items") == "f'/cart/{cart_id}/items'"
    assert _python_value({"cart_id": "{cart_id}", "qty": 2}) == "{'cart_id': cart_id, 'qty': 2}"
    assert _python_value("plain") == "'plain'"


def test_python_path():
    assert _python_path("items.0.price") == "['items'][0]['price']"


CHECKOUT_JOURNEY = Journey(
    name="welcome checkout",
    steps=[
        JourneyStep("create cart", "POST", "/cart", capture={"cart_id": "id"}),
        JourneyStep("add mug", "POST", "/cart/{cart_id}/items", body={"product_id": "mug", "quantity": 2}),
        JourneyStep("checkout", "POST", "/checkout", body={"cart_id": "{cart_id}"}),
    ],
)


def _checkout_result(tax):
    steps = [StepResult("checkout", "POST", "/checkout", 200, {"tax": tax}, 1.0)]
    return RehearsalResult([JourneyRun(CHECKOUT_JOURNEY.name, "baseline", steps)], [])


def _run_generated(path):
    namespace = {}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    tests = [fn for name, fn in namespace.items() if name.startswith("test_")]
    assert tests
    for test in tests:
        test()


def test_generated_test_passes_when_app_matches_baseline(tmp_path):
    # demo_app on main: 2 mugs = 24.00, tax 1.92
    differences = [diff(R, journey=CHECKOUT_JOURNEY.name, field="tax", baseline=1.92, candidate=2.40)]
    [path] = generate_tests(differences, _checkout_result(1.92), [CHECKOUT_JOURNEY], Config(), "main", "feature",
                            tmp_path, NOW)
    assert "assert body['tax'] == 1.92  # feature returned 2.4" in path.read_text()
    _run_generated(path)


def test_generated_test_fails_when_app_differs_from_baseline(tmp_path):
    differences = [diff(R, journey=CHECKOUT_JOURNEY.name, field="tax", baseline=9.99, candidate=1.92)]
    [path] = generate_tests(differences, _checkout_result(9.99), [CHECKOUT_JOURNEY], Config(), "main", "feature",
                            tmp_path, NOW)
    with pytest.raises(AssertionError):
        _run_generated(path)


def test_no_tests_without_regressions(tmp_path):
    assert generate_tests([diff(S)], _checkout_result(1.0), [CHECKOUT_JOURNEY], Config(), "a", "b", tmp_path, NOW) == []


# --- end to end in a throwaway git repo ---------------------------------------------------


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "app.txt").write_text("v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.txt").write_text("v2\n")
    _git(repo, "commit", "-q", "-am", "Change app to v2")
    _git(repo, "checkout", "-q", "main")
    return repo


def test_rollback_plan_lists_commits_and_uses_last_release_tag(repo):
    plan = rollback_plan("main", "feature", repo)
    assert "Change app to v2" in plan
    assert "git revert --no-edit" in plan

    _git(repo, "tag", "-a", "release/v1", "-m", "v1", "main")
    assert "the last promoted release `release/v1`" in rollback_plan("main", "feature", repo)


def test_promote_release_tags_once(repo):
    tag = agentia.promote_release("feature", "reports/r.md", repo)
    assert tag.startswith("release/")
    assert agentia.promote_release("feature", "reports/r.md", repo) == tag
    assert _git(repo, "tag", "--list", "release/*") == tag


def test_judge_blocks_and_writes_report_tests_and_rollback(repo):
    config = Config()
    differences = [
        diff(R, journey=CHECKOUT_JOURNEY.name, field="tax", baseline=1.92, candidate=2.40),
        diff(I, journey="save10", step="checkout", field="status_code", baseline=400, candidate=200),
    ]
    decision = judge(_checkout_result(1.92), differences, [CHECKOUT_JOURNEY], config, "main", "feature",
                     repo_root=repo, timestamp=NOW)

    assert decision.verdict == Verdict.BLOCK
    assert decision.score == 75
    assert decision.report_path == "reports/rehearsal-20261006-120000.md"
    assert decision.generated_tests == ["tests/generated/test_regression_welcome_checkout.py"]
    assert decision.release_tag is None
    assert _git(repo, "tag", "--list") == ""

    report = (repo / decision.report_path).read_text()
    for expected in ("❌ BLOCK", "75/100", "## Behavior differences", "❌ regression", "✅ intended",
                     "## Generated regression tests", "## Rollback plan", "Not analyzed."):
        assert expected in report


def test_judge_promotes_clean_change(repo):
    differences = [diff(I, field="status_code", baseline=400, candidate=200)]
    decision = judge(_checkout_result(1.92), differences, [CHECKOUT_JOURNEY], Config(), "main", "feature",
                     repo_root=repo, timestamp=NOW)

    assert decision.verdict == Verdict.PROMOTE
    assert decision.release_tag == _git(repo, "tag", "--list", "release/*")
    assert decision.generated_tests == [] and decision.rollback_plan is None
    report = (repo / decision.report_path).read_text()
    assert "✅ PROMOTE" in report and f"Promoted as `{decision.release_tag}`" in report
