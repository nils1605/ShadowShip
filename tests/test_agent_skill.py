"""The Agentia Agent Skill must stay loadable and only mention commands that exist."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".agents/skills/shadowship-rehearsal/SKILL.md"


def _frontmatter(text: str) -> dict:
    _, header, _ = text.split("---", 2)
    return yaml.safe_load(header)


def test_skill_has_valid_frontmatter():
    meta = _frontmatter(SKILL.read_text())
    assert meta["name"] == SKILL.parent.name
    assert "quality gate" in meta["description"]


def test_skill_is_linked_for_claude_and_cursor():
    for target in (".claude", ".cursor"):
        linked = ROOT / target / "skills/shadowship-rehearsal/SKILL.md"
        assert linked.read_text() == SKILL.read_text()


def test_skill_references_real_files_and_commands():
    text = SKILL.read_text()
    assert (ROOT / ".agentia_quality_gates.sh").exists()
    from shadowship.cli import app

    commands = {command.name or command.callback.__name__ for command in app.registered_commands}
    for command in ("rehearse", "report"):
        assert f"shadowship {command}" in text
        assert command in commands
