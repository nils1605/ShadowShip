"""Small helper for running git commands."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo_root: str | Path, *args: str) -> str:
    """Run `git <args>` in `repo_root` and return stripped stdout. Raises GitError on failure."""
    result = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def short_sha(repo_root: str | Path, ref: str) -> str:
    return git(repo_root, "rev-parse", "--short", f"{ref}^{{commit}}")
