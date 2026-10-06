#!/usr/bin/env bash
# ShadowShip demo: the whole release-rehearsal story in five acts.
#
#   scripts/demo.sh              pause between acts (for presenting)
#   NO_PAUSE=1 scripts/demo.sh   run straight through
#
# Optional: export ANTHROPIC_API_KEY first for AI reasoning. Without it, rule-based fallbacks are used.
set -euo pipefail
cd "$(dirname "$0")/.."

BUGGY=feature/discount-change
FIXED=feature/discount-fixed
SHADOWSHIP="$PWD/.venv/bin/shadowship"
PYTEST="$PWD/.venv/bin/pytest"
[ -x "$SHADOWSHIP" ] || SHADOWSHIP=shadowship
[ -x "$PYTEST" ] || PYTEST=pytest
SCRATCH=.shadowship/demo  # gitignored

act() {
  echo
  printf '\033[1;36m━━━ %s\033[0m\n' "$1"
  if [ -z "${NO_PAUSE:-}" ]; then read -r -p "    press Enter ▸ " _; fi
}

# Run a command inside a temporary checkout of a branch.
on_branch() {
  local branch=$1; shift
  git worktree add -q --detach "$SCRATCH/wt" "$branch"
  if [ -d tests/generated ]; then
    mkdir -p "$SCRATCH/wt/tests/generated"
    cp tests/generated/*.py "$SCRATCH/wt/tests/generated/"
  fi
  (cd "$SCRATCH/wt" && "$@") || true
  git worktree remove --force "$SCRATCH/wt"
}

cleanup() { git worktree remove --force "$SCRATCH/wt" 2>/dev/null || true; }
trap cleanup EXIT

# Fresh start: forget the release tag and generated tests from a previous demo run.
git tag -d "release/$(git rev-parse --short "$FIXED")" >/dev/null 2>&1 || true
rm -rf tests/generated
mkdir -p "$SCRATCH"

act "1. A developer adds a SAVE10 discount code on $BUGGY. The unit tests pass."
git log --oneline "main..$BUGGY"
on_branch "$BUGGY" "$PYTEST" -q demo_app/tests

act "2. Before it ships, ShadowShip rehearses the release."
if "$SHADOWSHIP" rehearse --base main --head "$BUGGY"; then
  echo "(unexpected: the buggy branch was promoted)"
fi

act "3. The generated regression test pins the bug: it passes on main and fails on $BUGGY."
sed -n '/^def test_/,$p' tests/generated/test_regression_small_cart_welcome_discount.py
echo
echo "On main:"
"$PYTEST" -q --tb=line -p no:cacheprovider tests/generated | tail -1
echo "On $BUGGY:"
on_branch "$BUGGY" "$PYTEST" -q --tb=line -p no:cacheprovider tests/generated

act "4. The developer fixes the bug on $FIXED and rehearses again."
git log --oneline "$BUGGY..$FIXED"
"$SHADOWSHIP" rehearse --base main --head "$FIXED"

act "5. Promoted. The release is tagged and the report is ready."
git tag --list 'release/*' -n1
echo "Latest report: $(ls -t reports/rehearsal-*.md | head -1)   (view it with: shadowship report)"
