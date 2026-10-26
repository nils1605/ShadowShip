---
name: shadowship-rehearsal
description: Use this skill before a change is submitted, promoted, or released, and whenever the user asks whether a branch or user story is safe to ship. ShadowShip rehearses the release by running the baseline and candidate versions side by side, replaying real user journeys against both, and blocking regressions that unit tests miss. It runs as the Agentia local quality gate (.agentia_quality_gates.sh), so it also covers `agentia cicd work test` and `agentia cicd work submit`.
---

# ShadowShip release rehearsal

ShadowShip answers one question: **is this change safe to release?** It starts the current version
(baseline) and the proposed version (candidate) side by side, replays the journeys in `journeys/*.yaml`
against both, compares the responses, and returns a verdict:

- **PROMOTE** (exit code 0): only intended changes were found. Safe to ship.
- **BLOCK** (exit code 1): at least one regression, or confidence below the threshold in `shadowship.yaml`.

In this repo it is wired in as the Agentia local quality gate, so `agentia cicd work submit` refuses to
promote a story that ShadowShip blocks.

## Start Here

1. Make sure the working tree is committed. ShadowShip rehearses git refs, not uncommitted edits.
2. Run the rehearsal. Prefer the Agentia path when the user works with Copado user stories:
   - Through Agentia: `agentia cicd work test` (runs `.agentia_quality_gates.sh`, which runs ShadowShip
     against `main`; set `SHADOWSHIP_BASE` for another base branch).
   - Directly: `shadowship rehearse --base main --head <branch-or-HEAD>` (use `.venv/bin/shadowship` if
     `shadowship` is not on PATH).
3. Read the verdict from the exit code and the final "Verdict" panel. Show the full report with
   `shadowship report` (latest) or `shadowship report reports/rehearsal-<timestamp>.md`.

## On BLOCK

1. Tell the user it is blocked, the confidence score, and each regression from the differences table:
   journey, step, field, baseline value, candidate value.
2. Point to the generated regression tests in `tests/generated/` (they assert the baseline behavior and
   fail on the candidate) and the rollback plan in the report.
3. Fix the code so the generated tests pass, commit, and rehearse again.
4. Do not submit or promote a blocked change. Do not use `agentia cicd work submit --skip-local-tests`
   to get around the gate unless the user explicitly asks for it and accepts the risk.

## On PROMOTE

1. Tell the user the change is approved, the confidence score, and the `release/<sha>` tag ShadowShip
   created.
2. Only run `agentia cicd work submit --done` if the user asked to promote or finish the story. Submit
   runs this gate again, which is expected.

## Rules

- Each `suspicious` difference costs 10 points. One won't block by itself, but several can push the score
  below the threshold. Mention them so a human can confirm they were intended.
- Without `ANTHROPIC_API_KEY`, ShadowShip uses rule-based fallbacks. The verdict is still valid. Don't
  report the missing key as a failure.
- Never edit `journeys/*.yaml` or `shadowship.yaml` to make a blocked change pass.
- If the rehearsal itself fails (a server didn't start, a port was taken), report the error. It is not
  a BLOCK verdict about the code.

## Report Back

Report the command used, base and head refs, verdict, confidence score, regressions (if any), the report
path, and any generated tests or release tag.
