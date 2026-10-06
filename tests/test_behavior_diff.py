from shadowship.agents.behavior_diff import MISSING, body_differences, compare_journey, compare_runs
from shadowship.config import DiffConfig
from shadowship.models import Classification, ImpactReport, JourneyRun, RehearsalResult, StepResult

CONFIG = DiffConfig()


def step(name="checkout", status=200, body=None, latency=5.0, error=None, path="/checkout"):
    return StepResult(name, "POST", path, status, body if body is not None else {}, latency, error)


def runs(baseline_steps, candidate_steps, journey="j"):
    return JourneyRun(journey, "baseline", baseline_steps), JourneyRun(journey, "candidate", candidate_steps)


def classes(differences):
    return [(d.field, d.classification) for d in differences]


# --- body_differences ------------------------------------------------------------


def test_body_differences_ignores_volatile_fields_at_any_depth():
    baseline = {"id": "a", "cart_id": "x", "total": 10, "items": [{"id": "1", "qty": 1}]}
    candidate = {"id": "b", "cart_id": "y", "total": 10, "items": [{"id": "2", "qty": 1}]}
    assert body_differences(baseline, candidate, CONFIG.ignore_fields) == []


def test_body_differences_reports_changed_added_and_removed_fields():
    baseline = {"tax": 8.32, "old": 1, "items": [{"qty": 1}]}
    candidate = {"tax": 8.72, "new": 2, "items": [{"qty": 2}]}
    assert body_differences(baseline, candidate, []) == [
        ("items.0.qty", 1, 2),
        ("new", MISSING, 2),
        ("old", 1, MISSING),
        ("tax", 8.32, 8.72),
    ]


# --- classification rules ---------------------------------------------------------


def test_identical_runs_have_no_differences():
    b, c = runs([step(body={"total": 1})], [step(body={"total": 1})])
    assert compare_journey(b, c, CONFIG) == []


def test_candidate_server_error_is_regression():
    b, c = runs([step(status=200)], [step(status=500)])
    assert classes(compare_journey(b, c, CONFIG)) == [("status_code", Classification.REGRESSION)]


def test_candidate_connection_failure_is_regression():
    b, c = runs([step(status=200)], [step(status=None, error="request failed")])
    assert classes(compare_journey(b, c, CONFIG)) == [("status_code", Classification.REGRESSION)]


def test_newly_accepted_request_is_intended():
    b, c = runs([step(status=400, body={"detail": "Invalid"})], [step(status=200, body={"total": 9})])
    assert classes(compare_journey(b, c, CONFIG)) == [("status_code", Classification.INTENDED)]


def test_newly_rejected_request_is_regression():
    b, c = runs([step(status=200)], [step(status=400)])
    assert classes(compare_journey(b, c, CONFIG)) == [("status_code", Classification.REGRESSION)]


def test_other_status_change_is_suspicious():
    b, c = runs([step(status=404)], [step(status=400)])
    assert classes(compare_journey(b, c, CONFIG)) == [("status_code", Classification.SUSPICIOUS)]


def test_changed_error_detail_is_suspicious():
    b, c = runs([step(status=400, body={"detail": "a"})], [step(status=400, body={"detail": "b"})])
    assert classes(compare_journey(b, c, CONFIG)) == [("detail", Classification.SUSPICIOUS)]


def test_changed_output_in_existing_journey_is_regression():
    b, c = runs([step(body={"tax": 8.32})], [step(body={"tax": 8.72})])
    [difference] = compare_journey(b, c, CONFIG)
    assert difference.classification == Classification.REGRESSION
    assert (difference.baseline, difference.candidate) == (8.32, 8.72)


def test_changed_output_in_journey_using_new_behavior_is_suspicious():
    b, c = runs(
        [step("add item", body={"subtotal": 10}), step(status=400)],
        [step("add item", body={"subtotal": 11}), step(status=200)],
    )
    assert classes(compare_journey(b, c, CONFIG)) == [
        ("subtotal", Classification.SUSPICIOUS),
        ("status_code", Classification.INTENDED),
    ]


def test_reason_mentions_blast_radius_when_impact_is_known():
    b, c = runs([step(body={"tax": 1})], [step(body={"tax": 2})])
    inside = ImpactReport(["demo_app/main.py"], ["POST /checkout"], "Add SAVE10")
    outside = ImpactReport(["demo_app/main.py"], ["GET /products"], "Add SAVE10")
    assert "in the blast radius" in compare_journey(b, c, CONFIG, inside)[0].reason
    assert "outside the blast radius" in compare_journey(b, c, CONFIG, outside)[0].reason


# --- latency -------------------------------------------------------------------------


def test_large_slowdown_is_suspicious():
    b, c = runs([step(latency=100.0)], [step(latency=400.0)])
    [difference] = compare_journey(b, c, CONFIG)
    assert difference.field == "latency_ms"
    assert difference.classification == Classification.SUSPICIOUS


def test_small_absolute_slowdown_is_ignored():
    b, c = runs([step(latency=2.0)], [step(latency=8.0)])  # +300% but only 6 ms
    assert compare_journey(b, c, CONFIG) == []


def test_small_relative_slowdown_is_ignored():
    b, c = runs([step(latency=1000.0)], [step(latency=1200.0)])  # +200 ms but only 20%
    assert compare_journey(b, c, CONFIG) == []


# --- the demo scenario ------------------------------------------------------------------


def _checkout_run(target, discount_code_status, tax, total):
    welcome = JourneyRun("welcome", target, [
        step("create cart", body={"id": f"{target}-cart"}, path="/cart"),
        step("checkout", body={"cart_id": f"{target}-cart", "subtotal": 109.0, "discount": 5.0, "tax": tax,
                               "total": total}),
    ])
    save10 = JourneyRun("save10", target, [
        step("create cart", body={"id": f"{target}-cart"}, path="/cart"),
        step("checkout", status=discount_code_status, body={"detail": "Invalid discount code"}
             if discount_code_status == 400 else {"discount": 10.9}),
    ])
    return [welcome, save10]


def test_demo_bug_is_caught_and_new_code_is_intended():
    result = RehearsalResult(
        journey_runs_baseline=_checkout_run("baseline", 400, 8.32, 112.32),
        journey_runs_candidate=_checkout_run("candidate", 200, 8.72, 112.72),
    )
    found = {(d.journey, d.field): d.classification for d in compare_runs(result, CONFIG)}
    assert found == {
        ("welcome", "tax"): Classification.REGRESSION,
        ("welcome", "total"): Classification.REGRESSION,
        ("save10", "status_code"): Classification.INTENDED,
    }


def test_journeys_only_in_one_run_are_skipped():
    result = RehearsalResult(
        journey_runs_baseline=[JourneyRun("only-baseline", "baseline", [step()])],
        journey_runs_candidate=[],
    )
    assert compare_runs(result, CONFIG) == []
