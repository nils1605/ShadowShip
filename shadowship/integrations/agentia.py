"""All Agentia Headless calls live here.

We don't have the Agentia Headless CLI/plugin docs yet, so every function is a logged
stub or a local equivalent. When the docs arrive, replace the bodies here and nowhere else.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shadowship.gitutil import git, short_sha

log = logging.getLogger("shadowship.agentia")


def trigger_pipeline(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Tell Agentia a pipeline stage started or finished. Stub: logs only."""
    log.info("[agentia stub] trigger_pipeline stage=%s payload=%s", stage, payload)
    return {"stage": stage, "status": "logged"}


def provision_environment(ref: str) -> dict[str, Any]:
    """Ask Agentia for an environment running `ref`. Stub: the Rehearsal agent uses local git worktrees."""
    log.info("[agentia stub] provision_environment ref=%s (using local git worktree)", ref)
    return {"ref": ref, "provider": "local-worktree"}


def promote_release(ref: str, report_path: str | Path, repo_root: str | Path = ".") -> str:
    """Promote `ref` to a release. Local equivalent: create the git tag `release/<sha>`.

    Returns the tag name. Promoting the same commit twice reuses the existing tag.
    """
    tag = f"release/{short_sha(repo_root, ref)}"
    existing = git(repo_root, "tag", "--list", tag)
    if existing:
        log.info("[agentia stub] promote_release: %s already exists", tag)
        return tag
    git(repo_root, "tag", "-a", tag, f"{ref}^{{commit}}", "-m", f"Promoted by ShadowShip. Report: {report_path}")
    log.info("[agentia stub] promote_release: tagged %s as %s", ref, tag)
    return tag


def post_report(report_path: str | Path) -> dict[str, Any]:
    """Publish the Release Rehearsal Report to Agentia. Stub: logs only."""
    log.info("[agentia stub] post_report %s", report_path)
    return {"report": str(report_path), "status": "logged"}
