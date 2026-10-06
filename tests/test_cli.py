from typer.testing import CliRunner

from shadowship.agents.rehearsal import load_journeys
from shadowship.cli import app

runner = CliRunner()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("rehearse", "report", "init"):
        assert command in result.output


def test_init_writes_config_and_example_journey_and_refuses_overwrite(tmp_path):
    path = tmp_path / "shadowship.yaml"
    result = runner.invoke(app, ["init", "--config", str(path)])
    assert result.exit_code == 0
    assert path.exists()
    [journey] = load_journeys(tmp_path / "journeys")
    assert journey.name == "example-health" and journey.steps[0].path == "/health"

    result = runner.invoke(app, ["init", "--config", str(path)])
    assert result.exit_code == 1


def test_rehearse_reports_config_errors(tmp_path):
    path = tmp_path / "shadowship.yaml"
    path.write_text("bogus: 1\n")
    result = runner.invoke(app, ["rehearse", "--head", "feature/x", "--config", str(path)])
    assert result.exit_code == 2
    assert "bogus" in result.output


def test_report_shows_latest(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "rehearsal-20261001-000000.md").write_text("# Old report\n")
    (reports / "rehearsal-20261002-000000.md").write_text("# New report\n")
    config = tmp_path / "shadowship.yaml"
    config.write_text(f"reports_dir: {reports}\n")

    result = runner.invoke(app, ["report", "--config", str(config)])
    assert result.exit_code == 0
    assert "New report" in result.output
