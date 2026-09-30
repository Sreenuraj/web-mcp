import tomllib
from pathlib import Path

import pytest

from web_mcp.__main__ import build_parser
from web_mcp.config import CONFIG_TEMPLATE, Config, ConfigError, load_config


def test_template_matches_defaults():
    assert Config.model_validate(tomllib.loads(CONFIG_TEMPLATE)) == Config()
    assert (Path(__file__).parent.parent / "config.example.toml").read_text() == CONFIG_TEMPLATE


def test_layering_and_env_override(tmp_path):
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.write_text("[limits]\ndaily_searches = 5\nmax_results = 3\n")
    b.write_text("[limits]\nmax_results = 7\n[policy]\ndrift_action = 'block'\n")
    c = load_config([a, b], env={"WEBMCP_LIMITS__DAILY_FETCHES": "9", "WEBMCP_DOMAINS__DENY": '["x.com"]', "OTHER": "1"})
    assert c.limits.daily_searches == 5 and c.limits.max_results == 7 and c.limits.daily_fetches == 9
    assert c.policy.drift_action == "block" and c.domains.deny == ["x.com"]


def test_invalid_config_is_a_clear_error(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[policy]\ndrift_action = 'sometimes'\n")
    with pytest.raises(ConfigError):
        load_config([bad], env={})
    typo = tmp_path / "typo.toml"
    typo.write_text("[limits]\ndaily_serches = 5\n")  # unknown keys must not be silently ignored
    with pytest.raises(ConfigError):
        load_config([typo], env={})
    broken = tmp_path / "broken.toml"
    broken.write_text("[limits\n")
    with pytest.raises(ConfigError):
        load_config([broken], env={})


def test_cli_parser_has_all_commands():
    p = build_parser()
    for cmd in ("serve", "approve", "audit", "config", "doctor", "key", "searxng", "install-client", "cache-clear"):
        assert p.parse_args([cmd] + (["set", "brave"] if cmd == "key" else ["up"] if cmd == "searxng" else [])).fn


def test_old_settings_are_migrated_not_fatal(tmp_path, capsys):
    old = tmp_path / "old.toml"
    old.write_text('[approval]\nmodes = ["elicitation", "os_dialog", "deny"]\ncli_wait_seconds = 45\n')
    c = load_config([old], env={})
    assert c.approval.modes == ["elicitation", "relay", "deny"]
    err = capsys.readouterr().err
    assert "os_dialog" in err and "cli_wait_seconds" in err
