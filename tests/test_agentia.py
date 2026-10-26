import json
import subprocess

import pytest

from shadowship.config import AgentiaConfig
from shadowship.integrations import agentia


class FakeAgentia:
    """Stands in for the `agentia` CLI: records calls and answers with canned JSON."""

    def __init__(self, responses=None, returncode=0):
        self.calls = []
        self.responses = responses or {}
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        key = " ".join(cmd[1:4])
        result = self.responses.get(key, {})
        return subprocess.CompletedProcess(cmd, self.returncode, json.dumps({"status": 0, "result": result}), "")


@pytest.fixture
def fake(monkeypatch):
    fake = FakeAgentia({"cicd work get": {"Name": "US-0042", "copado__User_Story_Title__c": "Add discount code"}})
    monkeypatch.setattr(agentia.subprocess, "run", fake)
    monkeypatch.setattr(agentia, "cli_ready", lambda: True)
    return fake


def test_falls_back_to_local_when_cli_not_ready(monkeypatch):
    monkeypatch.setattr(agentia, "cli_ready", lambda: False)
    assert agentia.trigger_pipeline("rehearse", {}, AgentiaConfig())["status"] == "logged"
    assert agentia.post_report("r.md", "BLOCK", AgentiaConfig(report_field="technical-specifications"))["status"] == "logged"


def test_disabled_config_stays_local(fake):
    assert agentia.trigger_pipeline("rehearse", {}, AgentiaConfig(enabled=False))["status"] == "logged"
    assert fake.calls == []


def test_trigger_pipeline_reads_the_user_story(fake):
    event = agentia.trigger_pipeline("rehearse", {}, AgentiaConfig(user_story="US-0042"))
    assert event["status"] == "ok"
    assert event["user_story"] == "US-0042 · Add discount code"
    assert fake.calls == [["agentia", "cicd", "work", "get", "US-0042", "--json"]]


def test_blank_user_story_uses_active_work_item(fake):
    agentia.trigger_pipeline("rehearse", {}, AgentiaConfig())
    assert fake.calls == [["agentia", "cicd", "work", "get", "--json"]]


def test_post_report_needs_a_field(fake):
    assert agentia.post_report("r.md", "PROMOTE", AgentiaConfig())["status"] == "logged"
    assert fake.calls == []


def test_post_report_updates_the_user_story(fake):
    config = AgentiaConfig(user_story="US-0042", report_field="technical-specifications")
    assert agentia.post_report("reports/r.md", "BLOCK (40/100): tax bug", config)["status"] == "posted"
    cmd = fake.calls[0]
    assert cmd[:5] == ["agentia", "cicd", "work", "update", "US-0042"]
    assert cmd[5] == "--technical-specifications"
    assert "BLOCK (40/100): tax bug" in cmd[6] and "reports/r.md" in cmd[6]


def test_cli_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(agentia.subprocess, "run", FakeAgentia(returncode=1))
    monkeypatch.setattr(agentia, "cli_ready", lambda: True)
    event = agentia.trigger_pipeline("rehearse", {}, AgentiaConfig())
    assert event["status"] == "error"


def test_cli_ready_checks_the_cicd_credential(monkeypatch):
    agentia.cli_ready.cache_clear()
    monkeypatch.setattr(agentia.shutil, "which", lambda name: "/usr/local/bin/agentia")
    monkeypatch.setattr(agentia.subprocess, "run", FakeAgentia({"auth get --json": {"credentials": [
        {"type": "cicd", "set": True}]}}))
    assert agentia.cli_ready() is True
    agentia.cli_ready.cache_clear()
    monkeypatch.setattr(agentia.subprocess, "run", FakeAgentia({"auth get --json": {"credentials": [
        {"type": "cicd", "set": False}]}}))
    assert agentia.cli_ready() is False
    agentia.cli_ready.cache_clear()


def test_story_label_handles_missing_title():
    assert agentia.story_label({"Name": "US-1"}) == "US-1"
    assert agentia.story_label({}) == ""


def test_in_quality_gate(monkeypatch):
    monkeypatch.delenv(agentia.GATE_ENV_VAR, raising=False)
    assert not agentia.in_quality_gate()
    monkeypatch.setenv(agentia.GATE_ENV_VAR, "1")
    assert agentia.in_quality_gate()
