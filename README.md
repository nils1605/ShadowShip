# 🚢 ShadowShip

**Agents that rehearse your release before it ships.**

Unit tests check the pieces. ShadowShip checks the behavior. Before a change is promoted, it runs the
current version (**baseline**) and the proposed version (**candidate**) side by side, replays realistic user
journeys against both, compares what they do, and decides whether the change is safe to release.

```
shadowship rehearse --base main --head feature/discount-change
```

## The demo in one minute

A developer adds a `SAVE10` discount code. While doing it, they "simplify" checkout so that tax is computed
on the total **before** the discount. Every pricing function is still correct on its own, so **all unit
tests pass**. But customers using the existing `WELCOME5` code would now pay $0.40 too much tax.

ShadowShip catches it:

```
① Impact Scout · what could this change break?
   Affected routes: POST /checkout
   ⚠ Changed without any updated test referencing it: checkout.

② Rehearsal · run both versions side by side and replay journeys
   5 journeys, 23 requests against main (port 8001) and the branch (port 8002)

③ Behavior Diff · what behaves differently, and is it intended?
   large-cart-save10 › checkout with SAVE10      status_code    400 →    200   intended
   large-cart-welcome-discount › checkout ...    tax           8.32 →   8.72   regression
   small-cart-welcome-discount › checkout ...    total        29.16 →  29.56   regression

④ Release Judge · promote or block?
   ❌ BLOCK   Confidence 50/100
```

It then writes a **Release Rehearsal Report**, generates **pytest regression tests** that pass on `main`
and fail on the branch, and drafts a **rollback plan**. After the fix, the same command gives
`✅ PROMOTE 100/100` and tags the release.

Run the whole story yourself:

```bash
scripts/demo.sh              # pauses between acts; NO_PAUSE=1 to run straight through
```

## How it works

Four agents, each a small readable module in `shadowship/agents/`:

| Agent | Question | How |
|---|---|---|
| **Impact Scout** | What could this change break? | Parses the git diff with Python's `ast`, finds changed functions and settings, follows references until it reaches API route handlers, flags journeys that hit those routes, and adds risk hints. |
| **Rehearsal** | What do both versions actually do? | Checks out each ref into a git worktree, starts the app from each on its own port, replays every journey (passing captured values like cart ids between steps), and always cleans up. |
| **Behavior Diff** | What changed, and is it intended? | Compares responses field by field (ignoring ids and timestamps), flags slowdowns, and classifies each difference as `intended`, `suspicious`, or `regression`. |
| **Release Judge** | Promote or block? | Scores confidence from 100, blocks on any regression, writes the report, regression tests, and rollback plan, and promotes through the Agentia adapter. |

### Rule-based first, AI when available

Every LLM step has a rule-based fallback, so ShadowShip works with no API key at all. With
`ANTHROPIC_API_KEY` set (or an `ant auth login` profile), Claude adds:

- **Intent:** a summary of what the change means to do, plus the behavior changes it intends.
- **Classification:** refines the labels using that intent. Server errors always stay regressions.
- **Narrative:** a plain-language report summary.

The **verdict and score are always rule-based**, so the gate is deterministic and explainable.

### Classification rules (no LLM)

1. Candidate returns 5xx or fails where baseline worked → **regression**
2. Baseline rejected the request, candidate accepts it → **intended** (new behavior)
3. Baseline accepted the request, candidate rejects it → **regression**
4. Output changed in an existing journey that worked on baseline → **regression**
5. Output changed in a journey that uses new behavior → **suspicious**
6. Much slower (over 50% and over 100 ms) → **suspicious**

## Quick start

```bash
uv venv --python 3.12 --managed-python .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

shadowship rehearse --base main --head feature/discount-change   # ❌ BLOCK, exit code 1
shadowship rehearse --base main --head feature/discount-fixed    # ✅ PROMOTE, exit code 0
shadowship report                                                # show the latest report
```

The exit code makes it a drop-in CI gate: non-zero means do not ship.

### Use it on your own service

```bash
shadowship init     # writes shadowship.yaml and journeys/example-health.yaml
```

Then set how to start your app and describe real user flows:

```yaml
# shadowship.yaml (excerpt)
app:
  start_command: python -m uvicorn myapp.main:app --port {port}
  asgi_app: myapp.main:app          # used by generated regression tests
  health_path: /health
diff:
  ignore_fields: [id, cart_id, created_at, timestamp]
judge:
  promote_threshold: 80
llm:
  model: claude-opus-5-5
  effort: low
```

```yaml
# journeys/checkout.yaml
name: checkout
steps:
  - name: create cart
    method: POST
    path: /cart
    capture: {cart_id: id}            # reuse as {cart_id} below
  - name: add mug
    method: POST
    path: /cart/{cart_id}/items
    body: {product_id: mug, quantity: 2}
  - name: checkout
    method: POST
    path: /checkout
    body: {cart_id: "{cart_id}", discount_code: WELCOME5}
```

## What you get

- `reports/rehearsal-<timestamp>.md`: verdict, score breakdown, blast radius, differences table,
  journeys replayed, generated tests, rollback plan.
- `reports/rehearsal-<timestamp>.json`: raw results for every request on both sides.
- `tests/generated/test_regression_*.py`: self-contained FastAPI `TestClient` tests asserting baseline
  behavior. Copy them into your test suite to lock the fix in.
- On PROMOTE: a `release/<sha>` git tag.

## Agentia Headless integration

All Agentia calls live in `shadowship/integrations/agentia.py` (`trigger_pipeline`,
`provision_environment`, `promote_release`, `post_report`). They are currently local stand-ins
(promotion creates a git tag) and will be replaced with real Agentia Headless calls.

## Project layout

```
shadowship/
  cli.py                 rehearse, report, init
  config.py              shadowship.yaml loading
  models.py              dataclasses shared between agents
  agents/                impact_scout, rehearsal, behavior_diff, release_judge
  integrations/          agentia (all Agentia calls), llm (Anthropic + fallbacks)
demo_app/                FastAPI demo shop (the service being rehearsed)
journeys/                user journeys replayed against both versions
scripts/demo.sh          the full demo story
tests/                   unit and end-to-end tests (pytest)
```

Demo branches: `feature/discount-change` (new code + hidden tax bug), `feature/discount-fixed` (bug fixed).

## Development

```bash
pytest                   # all tests, including end-to-end runs with real servers
pytest tests/generated   # regression tests generated by the last rehearsal
```

Built for the Agentia Headless Virtual Hackathon (October 2026).
