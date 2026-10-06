"""Loads `shadowship.yaml`.

Every setting has a sensible default, so a missing or partial config file
still works. Unknown keys are rejected to catch typos early.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("shadowship.yaml")


class ConfigError(ValueError):
    pass


@dataclass
class AppConfig:
    """How to start the service under test."""

    start_command: str = "python -m uvicorn demo_app.main:app --port {port}"
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
    model: str = "claude-sonnet-5-5"


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    journeys_dir: str = "journeys"
    reports_dir: str = "reports"
    generated_tests_dir: str = "tests/generated"
    worktrees_dir: str = ".shadowship"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SECTIONS = {"app": AppConfig, "diff": DiffConfig, "judge": JudgeConfig, "llm": LLMConfig}


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
