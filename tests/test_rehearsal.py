import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from shadowship.agents.rehearsal import (
    RehearsalError,
    extract,
    load_journeys,
    replay_journey,
    run_rehearsal,
    save_result,
    substitute,
    worktree,
)
from shadowship.config import Config
from shadowship.models import Journey, JourneyStep

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Pure helpers ------------------------------------------------------------


def test_substitute_keeps_types_for_whole_placeholders():
    variables = {"cart_id": "abc", "qty": 3}
    assert substitute("/cart/{cart_id}/items", variables) == "/cart/abc/items"
    assert substitute({"cart_id": "{cart_id}", "quantity": "{qty}"}, variables) == {"cart_id": "abc", "quantity": 3}
    assert substitute(["{qty}", 1], variables) == [3, 1]
    assert substitute(None, variables) is None


def test_substitute_unknown_placeholder_raises():
    with pytest.raises(KeyError):
        substitute("/cart/{nope}", {})


def test_extract_dotted_path():
    body = {"id": "abc", "items": [{"price": 12.0}]}
    assert extract(body, "id") == "abc"
    assert extract(body, "items.0.price") == 12.0


def test_load_journeys_from_repo():
    journeys = load_journeys(REPO_ROOT / "journeys")
    names = [j.name for j in journeys]
    assert "large-cart-welcome-discount" in names
    checkout = next(j for j in journeys if j.name == "small-cart-checkout")
    assert checkout.steps[0].capture == {"cart_id": "id"}


# --- Replaying against a fake server -------------------------------------------


def _fake_shop(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/cart":
        return httpx.Response(200, json={"id": "cart-1"})
    if request.url.path == "/cart/cart-1/items":
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/checkout":
        body = json.loads(request.content)
        return httpx.Response(200, json={"cart_id": body["cart_id"], "total": 25.92})
    return httpx.Response(404, json={"detail": "not found"})


CHECKOUT = Journey(
    name="checkout",
    steps=[
        JourneyStep("create cart", "POST", "/cart", capture={"cart_id": "id"}),
        JourneyStep("add item", "POST", "/cart/{cart_id}/items", body={"product_id": "mug"}),
        JourneyStep("checkout", "POST", "/checkout", body={"cart_id": "{cart_id}"}),
    ],
)


def test_replay_passes_captured_values_between_steps():
    client = httpx.Client(transport=httpx.MockTransport(_fake_shop), base_url="http://shop")
    run = replay_journey(client, CHECKOUT, "baseline")

    assert run.target == "baseline"
    assert [s.status_code for s in run.steps] == [200, 200, 200]
    assert run.steps[1].path == "/cart/cart-1/items"
    assert run.steps[2].body == {"cart_id": "cart-1", "total": 25.92}
    assert all(s.error is None for s in run.steps)


def test_replay_records_failed_capture_and_continues():
    def no_id(request):
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(no_id), base_url="http://shop")
    run = replay_journey(client, CHECKOUT, "candidate")

    assert run.steps[0].status_code == 500
    assert "could not capture" in run.steps[0].error
    assert "missing value" in run.steps[1].error
    assert len(run.steps) == 3


def test_replay_records_connection_errors():
    def refuse(request):
        raise httpx.ConnectError("refused")

    client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://shop")
    run = replay_journey(client, CHECKOUT, "candidate")
    assert run.steps[0].status_code is None
    assert "request failed" in run.steps[0].error


# --- End to end: real git worktrees and real servers ---------------------------


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args], cwd=repo, check=True,
                   capture_output=True)


@pytest.fixture
def shop_repo(tmp_path):
    """A throwaway repo with the demo app on `main` and a tax change on `candidate`."""
    repo = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "demo_app", repo / "demo_app", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    (repo / "journeys").mkdir()
    shutil.copy(REPO_ROOT / "journeys" / "small-cart-checkout.yaml", repo / "journeys")
    (repo / ".gitignore").write_text(".shadowship/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    _git(repo, "checkout", "-q", "-b", "candidate")
    pricing = repo / "demo_app" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("TAX_RATE = 0.08", "TAX_RATE = 0.10"))
    _git(repo, "commit", "-q", "-am", "raise tax")
    _git(repo, "checkout", "-q", "main")
    return repo


def _test_config() -> Config:
    config = Config()
    config.app.baseline_port = 8111
    config.app.candidate_port = 8112
    return config


def test_run_rehearsal_end_to_end(shop_repo, tmp_path):
    result = run_rehearsal("main", "candidate", _test_config(), repo_root=shop_repo)

    baseline = result.journey_runs_baseline[0].steps[-1]
    candidate = result.journey_runs_candidate[0].steps[-1]
    assert baseline.status_code == candidate.status_code == 200
    assert baseline.body["tax"] == 1.92
    assert candidate.body["tax"] == 2.40

    # Worktrees are cleaned up and servers are stopped.
    assert not (shop_repo / ".shadowship" / "baseline").exists()
    assert not (shop_repo / ".shadowship" / "candidate").exists()
    with pytest.raises(httpx.ConnectError):
        httpx.get("http://127.0.0.1:8111/health")

    saved = json.loads(save_result(result, tmp_path / "reports", "main", "candidate").read_text())
    assert saved["head"] == "candidate"
    assert saved["journey_runs_candidate"][0]["steps"][-1]["body"]["tax"] == 2.40


def test_run_rehearsal_cleans_up_when_server_fails(shop_repo):
    config = _test_config()
    config.app.start_command = "python -c 'raise SystemExit(1)'"
    with pytest.raises(RehearsalError, match="exited"):
        run_rehearsal("main", "candidate", config, repo_root=shop_repo)
    assert not (shop_repo / ".shadowship" / "baseline").exists()


def test_unknown_ref_is_reported(shop_repo):
    with pytest.raises(RehearsalError, match="rev-parse"):
        with worktree(shop_repo, "no-such-branch", shop_repo / ".shadowship" / "x"):
            pass
