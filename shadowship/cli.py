"""ShadowShip command line interface."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from shadowship import __version__
from shadowship.agents.behavior_diff import compare_runs
from shadowship.agents.impact_scout import scout
from shadowship.agents.rehearsal import RehearsalError, load_journeys, run_rehearsal, save_result
from shadowship.agents.release_judge import judge
from shadowship.config import DEFAULT_CONFIG_PATH, ConfigError, default_config_yaml, load_config
from shadowship.gitutil import GitError
from shadowship.models import (
    BehaviorDifference,
    Classification,
    ImpactReport,
    JourneyRun,
    JudgeDecision,
    RehearsalResult,
    Verdict,
)

app = typer.Typer(
    help="ShadowShip: agents that rehearse your release before it ships.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"shadowship {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version_callback, is_eager=True, help="Show version."),
) -> None:
    pass


@app.command()
def rehearse(
    base: str = typer.Option("main", "--base", help="Git ref of the current (baseline) version."),
    head: str = typer.Option(..., "--head", help="Git ref of the proposed (candidate) version."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to shadowship.yaml."),
) -> None:
    """Rehearse a release: run baseline and candidate side by side and judge the change."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2)

    console.print(
        Panel.fit(
            f"[bold]Baseline:[/bold] {base}\n[bold]Candidate:[/bold] {head}\n"
            f"[bold]Journeys:[/bold] {config.journeys_dir}/\n"
            f"[bold]Promote threshold:[/bold] {config.judge.promote_threshold}",
            title="ShadowShip release rehearsal",
        )
    )

    journeys = load_journeys(config.journeys_dir)
    impact = None
    try:
        with console.status("Mapping the blast radius..."):
            impact = scout(base, head, journeys=journeys)
        console.print(_impact_panel(impact))
    except GitError as exc:
        console.print(f"[yellow]Impact Scout skipped:[/yellow] {exc}")

    try:
        with console.status("Rehearsing...") as status:
            result = run_rehearsal(base, head, config, on_progress=lambda message: status.update(message))
    except RehearsalError as exc:
        console.print(f"[red]Rehearsal failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(_rehearsal_table(result, impact))

    differences = compare_runs(result, config.diff, impact)
    console.print(_differences_table(differences))

    timestamp = datetime.now()
    save_result(result, config.reports_dir, base, head, differences, timestamp)
    decision = judge(result, differences, journeys, config, base, head, impact=impact, timestamp=timestamp)
    console.print(_verdict_panel(decision))
    if decision.verdict == Verdict.BLOCK:
        raise typer.Exit(code=1)


def _impact_panel(impact: ImpactReport) -> Panel:
    lines = [
        f"[bold]Intent:[/bold] {impact.intent_summary}",
        f"[bold]Changed files:[/bold] {', '.join(impact.changed_files) or 'none'}",
        f"[bold]Affected routes:[/bold] {', '.join(impact.affected_routes) or 'none found'}",
    ]
    lines += [f"[yellow]⚠ {hint}[/yellow]" for hint in impact.risk_hints]
    return Panel("\n".join(lines), title="Impact Scout: blast radius", border_style="cyan")


def _verdict_panel(decision: JudgeDecision) -> Panel:
    promoted = decision.verdict == Verdict.PROMOTE
    color = "green" if promoted else "red"
    lines = [
        f"[bold {color}]{'✅' if promoted else '❌'} {decision.verdict.value}[/bold {color}]   "
        f"Confidence [bold]{decision.score}/100[/bold]",
        "",
        decision.summary,
        "",
        f"Report: [bold]{decision.report_path}[/bold]",
    ]
    if decision.release_tag:
        lines.append(f"Promoted as [bold]{decision.release_tag}[/bold]")
    for path in decision.generated_tests:
        lines.append(f"Generated test: [bold]{path}[/bold]")
    if decision.rollback_plan:
        lines.append("Rollback plan: see the report")
    return Panel("\n".join(lines), title="Release Judge", border_style=color)


CLASSIFICATION_STYLE = {
    Classification.INTENDED: "green",
    Classification.SUSPICIOUS: "yellow",
    Classification.REGRESSION: "bold red",
}


def _short(value: object, width: int = 30) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _differences_table(differences: list[BehaviorDifference]) -> Table | str:
    if not differences:
        return "[green]No behavior differences between baseline and candidate.[/green]"
    table = Table(title="Behavior differences")
    table.add_column("Journey")
    table.add_column("Step")
    table.add_column("Field")
    table.add_column("Baseline", justify="right")
    table.add_column("Candidate", justify="right")
    table.add_column("Class")
    for d in differences:
        style = CLASSIFICATION_STYLE[d.classification]
        table.add_row(
            d.journey, d.step, d.field, _short(d.baseline), _short(d.candidate),
            f"[{style}]{d.classification.value}[/{style}]",
        )
    return table


def _run_summary(run: JourneyRun) -> str:
    """Status codes counted per value, e.g. '200×6 400×1'. Red if any step errored or returned 5xx."""
    errors = [s for s in run.steps if s.error or s.status_code is None or s.status_code >= 500]
    counts = Counter(str(s.status_code or "ERR") for s in run.steps)
    statuses = " ".join(f"{status}×{count}" for status, count in sorted(counts.items()))
    color = "red" if errors else "green"
    return f"[{color}]{statuses}[/{color}]"


def _rehearsal_table(result: RehearsalResult, impact: ImpactReport | None = None) -> Table:
    relevant = set(impact.relevant_journeys) if impact else set()
    table = Table(title="Journeys replayed" + (" (★ = hits the blast radius)" if impact else ""))
    table.add_column("Journey")
    table.add_column("Steps", justify="right")
    table.add_column("Baseline")
    table.add_column("Candidate")
    for baseline, candidate in zip(result.journey_runs_baseline, result.journey_runs_candidate):
        name = ("★ " if baseline.journey in relevant else "  ") + baseline.journey if impact else baseline.journey
        table.add_row(name, str(len(baseline.steps)), _run_summary(baseline), _run_summary(candidate))
    return table


@app.command()
def report(
    path: Path = typer.Argument(None, help="Report to show. Defaults to the most recent one."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Path to shadowship.yaml."),
) -> None:
    """Show a Release Rehearsal Report in the terminal."""
    if path is None:
        reports_dir = Path(load_config(config_path).reports_dir)
        reports = sorted(reports_dir.glob("rehearsal-*.md"))
        if not reports:
            console.print(f"[yellow]No reports found in {reports_dir}/. Run `shadowship rehearse` first.[/yellow]")
            raise typer.Exit(code=1)
        path = reports[-1]
    if not path.exists():
        console.print(f"[red]Report not found:[/red] {path}")
        raise typer.Exit(code=1)
    console.print(Markdown(path.read_text()))


@app.command()
def init(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", "-c", help="Where to write the config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Create a shadowship.yaml with default settings."""
    if config_path.exists() and not force:
        console.print(f"[yellow]{config_path} already exists. Use --force to overwrite.[/yellow]")
        raise typer.Exit(code=1)
    config_path.write_text(default_config_yaml())
    console.print(f"[green]Wrote {config_path}[/green]")


if __name__ == "__main__":
    app()
