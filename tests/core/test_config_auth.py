"""Tests for auth-related config normalization helpers."""

import sys

sys.path.insert(0, "src")

from deepseek_web_api.core import config


class TestAuthConfig:
    def test_empty_config_returns_empty_tokens(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {})
        assert config.get_auth_tokens() == []

    def test_empty_auth_section_returns_empty_tokens(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"auth": {}})
        assert config.get_auth_tokens() == []

    def test_empty_tokens_list_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"auth": {"tokens": []}})
        assert config.get_auth_tokens() == []

    def test_valid_string_tokens_returned(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "CONFIG",
            {"auth": {"tokens": ["sk-xxx", "sk-yyy"]}},
        )
        assert config.get_auth_tokens() == ["sk-xxx", "sk-yyy"]

    def test_token_tables_return_enabled_values(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "CONFIG",
            {
                "auth": {
                    "tokens": [
                        {"name": "primary", "token": "sk-xxx", "enabled": True},
                        {"name": "backup", "token": "sk-yyy"},
                    ]
                }
            },
        )
        assert config.get_auth_tokens() == ["sk-xxx", "sk-yyy"]

    def test_disabled_token_tables_filtered_out(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "CONFIG",
            {
                "auth": {
                    "tokens": [
                        {"token": "sk-xxx", "enabled": False},
                        {"token": "sk-yyy", "enabled": True},
                    ]
                }
            },
        )
        assert config.get_auth_tokens() == ["sk-yyy"]

    def test_tokens_are_stripped(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "CONFIG",
            {"auth": {"tokens": ["  sk-xxx  ", "  sk-yyy  "]}},
        )
        assert config.get_auth_tokens() == ["sk-xxx", "sk-yyy"]

    def test_empty_strings_filtered_out(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "CONFIG",
            {"auth": {"tokens": ["sk-xxx", "", "   ", "sk-yyy"]}},
        )
        assert config.get_auth_tokens() == ["sk-xxx", "sk-yyy"]

    def test_non_list_auth_section_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"auth": {"tokens": "not-a-list"}})
        assert config.get_auth_tokens() == []

    def test_non_dict_auth_section_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"auth": "not-a-dict"})
        assert config.get_auth_tokens() == []

    def test_legacy_api_key_is_exposed_as_token(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"server": {"api_key": "legacy-key"}})
        monkeypatch.delenv("DEEPSEEK_WEB_API_KEY", raising=False)

        assert config.get_auth_tokens() == ["legacy-key"]

    def test_env_token_json_overrides_and_extends_config(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "CONFIG",
            {"auth": {"tokens": ["config-token"]}},
        )
        monkeypatch.setenv(
            "DEEPSEEK_WEB_AUTH_TOKENS_JSON",
            '[{"name":"prod","token":"env-token","enabled":true}]',
        )

        assert "env-token" in config.get_auth_tokens()
        assert "config-token" in config.get_auth_tokens()

    def test_auth_required_defaults_false(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"auth": {}})
        assert config.get_auth_required() is False

    def test_auth_required_reads_bool_like_values(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"auth": {"required": "true"}})
        assert config.get_auth_required() is True

    def test_server_host_prefers_env_override(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"server": {"host": "127.0.0.1"}})
        monkeypatch.setenv("DEEPSEEK_WEB_HOST", "0.0.0.0")
        assert config.get_server_host() == "0.0.0.0"

    def test_server_port_prefers_env_override(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"server": {"port": 5001}})
        monkeypatch.setenv("DEEPSEEK_WEB_PORT", "5101")
        assert config.get_server_port() == 5101

    def test_pool_size_prefers_env_override(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"server": {"pool_size": 10}})
        monkeypatch.setenv("DEEPSEEK_WEB_POOL_SIZE", "5")
        assert config.get_pool_size() == 5

    def test_pool_timeout_prefers_env_override(self, monkeypatch):
        monkeypatch.setattr(config, "CONFIG", {"server": {"pool_acquire_timeout": 30.0}})
        monkeypatch.setenv("DEEPSEEK_WEB_POOL_ACQUIRE_TIMEOUT", "12.5")
        assert config.get_pool_acquire_timeout() == 12.5
