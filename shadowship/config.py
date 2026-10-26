"""Loads `shadowship.yaml`.

Every setting has a sensible default, so a missing or partial config file
still works. Unknown keys are rejected to catch typos early.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import os

import yaml

DEFAULT_CONFIG_PATH = Path("shadowship.yaml")
DEFAULT_ENV_PATH = Path(".env")


class ConfigError(ValueError):
    pass


@dataclass
class AppConfig:
    """How to start the service under test."""

    start_command: str = "python -m uvicorn demo_app.main:app --port {port}"
    # ASGI app import path, used by generated regression tests to call the app in-process.
    asgi_app: str = "demo_app.main:app"
    health_path: str = "/health"
    baseline_port: int = 8001
    candidate_port: int = 8002
    startup_timeout_seconds: float = 20.0


@dataclass
class DiffConfig:
    # Fields whose values change on every run (ids, timestamps) and must not count as differences.
    ignore_fields: list[str] = field(default_factory=lambda: ["id", "cart_id", "created_at", "timestamp"])
    latency_threshold_pct: float = 50.0
    # Ignore slowdowns smaller than this; a 2 ms -> 4 ms jump is +100% but just noise.
    latency_min_increase_ms: float = 100.0


@dataclass
class JudgeConfig:
    promote_threshold: int = 80


@dataclass
class LLMConfig:
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "low"  # low | medium | high | xhigh | max; short classification/summaries need little


@dataclass
class AgentiaConfig:
    """Copado Agentia Headless CLI. Used only when `agentia` is installed and has a CICD API key."""

    enabled: bool = True
    # Copado user story to report on (ID or name). Blank = the active work item from `agentia cicd work set`.
    user_story: str = ""
    # User story field that receives the verdict summary, e.g. "technical-specifications". Blank = don't write.
    report_field: str = ""


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    agentia: AgentiaConfig = field(default_factory=AgentiaConfig)
    journeys_dir: str = "journeys"
    reports_dir: str = "reports"
    generated_tests_dir: str = "tests/generated"
    worktrees_dir: str = ".shadowship"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SECTIONS = {"app": AppConfig, "diff": DiffConfig, "judge": JudgeConfig, "llm": LLMConfig, "agentia": AgentiaConfig}


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"'{where}' must be a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{where}': {', '.join(sorted(unknown))}")
    return cls(**data)


def config_from_dict(data: dict[str, Any]) -> Config:
    data = dict(data)
    sections = {name: _build(cls, data.pop(name) or {}, name) for name, cls in _SECTIONS.items() if name in data}
    top = _build(Config, data, "shadowship.yaml")
    for name, value in sections.items():
        setattr(top, name, value)
    return top


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """Load config from `path`. Returns defaults if the file does not exist."""
    path = Path(path)
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return config_from_dict(data)


def default_config_yaml() -> str:
    """Default config rendered as YAML, used by `shadowship init`."""
    return yaml.safe_dump(Config().to_dict(), sort_keys=False)


def load_env_file(path: str | Path = DEFAULT_ENV_PATH) -> list[str]:
    """Load `KEY=value` lines from `path` into the environment, e.g. ANTHROPIC_API_KEY.

    Variables already set in the shell win. Blank values are skipped so an untouched copy of
    .env.example changes nothing. Returns the names that were set.
    """
    path = Path(path)
    if not path.exists():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
