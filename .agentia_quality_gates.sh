#!/usr/bin/env bash
# Agentia Headless local quality gate.
# `agentia cicd work submit` and `agentia cicd work test` run this before a user story is promoted.
# ShadowShip rehearses the release: exit 0 (PROMOTE) lets Agentia continue, exit 1 (BLOCK) stops it.
set -euo pipefail
cd "$(dirname "$0")"

BASE="${SHADOWSHIP_BASE:-main}"
SHADOWSHIP="${SHADOWSHIP_BIN:-shadowship}"
if [ -x .venv/bin/shadowship ] && [ -z "${SHADOWSHIP_BIN:-}" ]; then
  SHADOWSHIP=.venv/bin/shadowship
fi

export SHADOWSHIP_AGENTIA_GATE=1
# Unquoted on purpose: SHADOWSHIP_BIN may be a full command, e.g. "python -m shadowship.cli".
exec $SHADOWSHIP rehearse --base "$BASE" --head HEAD
