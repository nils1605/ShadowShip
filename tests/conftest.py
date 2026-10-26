import pytest

from shadowship import cli


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Tests never call the real Anthropic API, even when the developer has a key in .env or the shell."""
    monkeypatch.setattr(cli, "load_env_file", lambda *args, **kwargs: [])
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
