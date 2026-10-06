# ShadowShip

**Agents that rehearse your release before it ships.**

ShadowShip runs the current version (baseline) and the proposed version (candidate) of a service side by
side, replays realistic user journeys against both, compares behavior, and decides whether the change is
safe to release.

## Quick start

```bash
uv venv --python 3.12 --managed-python .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
shadowship --help
shadowship rehearse --base main --head feature/discount-change
```

LLM reasoning is optional: copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. Without a key,
every agent falls back to rule-based logic.

## Status

Work in progress for the Agentia Headless Virtual Hackathon.
