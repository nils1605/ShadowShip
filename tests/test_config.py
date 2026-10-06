import pytest

from shadowship.config import Config, ConfigError, config_from_dict, default_config_yaml, load_config


def test_missing_file_returns_defaults(tmp_path):
    config = load_config(tmp_path / "nope.yaml")
    assert config == Config()
    assert config.judge.promote_threshold == 80
    assert config.app.baseline_port != config.app.candidate_port


def test_partial_file_overrides_only_given_keys(tmp_path):
    path = tmp_path / "shadowship.yaml"
    path.write_text("judge:\n  promote_threshold: 90\nreports_dir: out\n")
    config = load_config(path)
    assert config.judge.promote_threshold == 90
    assert config.reports_dir == "out"
    assert config.diff.latency_threshold_pct == 50.0


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError, match="promote_treshold"):
        config_from_dict({"judge": {"promote_treshold": 90}})
    with pytest.raises(ConfigError, match="bogus"):
        config_from_dict({"bogus": 1})


def test_section_must_be_mapping():
    with pytest.raises(ConfigError, match="'judge' must be a mapping"):
        config_from_dict({"judge": 90})


def test_default_yaml_round_trips(tmp_path):
    path = tmp_path / "shadowship.yaml"
    path.write_text(default_config_yaml())
    assert load_config(path) == Config()


def test_repo_config_is_valid():
    load_config("shadowship.yaml")
