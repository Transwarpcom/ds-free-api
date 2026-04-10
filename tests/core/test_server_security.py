"""Tests for startup security warnings."""

import pytest

from deepseek_web_api.core import server_security


class TestServerSecurity:
    def test_is_loopback_host(self):
        assert server_security.is_loopback_host("127.0.0.1") is True
        assert server_security.is_loopback_host("localhost") is True
        assert server_security.is_loopback_host("[::1]") is True
        assert server_security.is_loopback_host("0.0.0.0") is False

    def test_validate_startup_config_loopback_no_tokens_passes(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "127.0.0.1")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: False)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: False)

        server_security.validate_startup_config()

    def test_validate_startup_config_non_loopback_with_tokens_passes(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "0.0.0.0")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: False)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: True)

        server_security.validate_startup_config()

    def test_validate_startup_config_non_loopback_with_required_auth_passes(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "0.0.0.0")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: True)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: True)

        server_security.validate_startup_config()

    def test_validate_startup_config_non_loopback_without_auth_fails(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "0.0.0.0")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: False)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: False)

        with pytest.raises(SystemExit) as exc_info:
            server_security.validate_startup_config()

        assert exc_info.value.code == 1

    def test_collect_startup_security_warnings_for_open_loopback(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "127.0.0.1")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: False)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: False)
        monkeypatch.setattr(server_security, "get_cors_origins", lambda: ["*"])

        warnings = server_security.collect_startup_security_warnings()

        assert any("Local API auth is disabled" in warning for warning in warnings)
        assert any("CORS allows all origins" in warning for warning in warnings)
        assert not any("not loopback" in warning for warning in warnings)

    def test_collect_startup_security_warnings_for_required_auth_without_tokens(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "127.0.0.1")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: True)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: False)
        monkeypatch.setattr(server_security, "get_cors_origins", lambda: ["https://app.example.com"])

        warnings = server_security.collect_startup_security_warnings()

        assert any("reject every request with 401" in warning for warning in warnings)

    def test_collect_startup_security_warnings_for_remote_with_auth(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "0.0.0.0")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: True)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: True)
        monkeypatch.setattr(server_security, "get_cors_origins", lambda: ["https://app.example.com"])

        warnings = server_security.collect_startup_security_warnings()

        assert any("not loopback" in warning for warning in warnings)
        assert not any("Local API auth is disabled" in warning for warning in warnings)

    def test_collect_startup_security_warnings_for_hardened_config(self, monkeypatch):
        monkeypatch.setattr(server_security, "get_server_host", lambda: "127.0.0.1")
        monkeypatch.setattr(server_security, "get_auth_required", lambda: True)
        monkeypatch.setattr(server_security, "has_effective_auth_tokens", lambda: True)
        monkeypatch.setattr(server_security, "get_cors_origins", lambda: ["https://app.example.com"])

        warnings = server_security.collect_startup_security_warnings()

        assert warnings == []

    def test_log_startup_security_warnings_prefers_info_when_enabled(self, monkeypatch):
        captured_info = []
        captured_warning = []

        monkeypatch.setattr(
            server_security,
            "get_auth_mode_summary",
            lambda: "Auth mode: formal auth.tokens mode; 2 enabled token(s); required=True.",
        )
        monkeypatch.setattr(server_security, "collect_startup_security_warnings", lambda: ["test warning"])
        monkeypatch.setattr(server_security.logger, "isEnabledFor", lambda level: True)
        monkeypatch.setattr(server_security.logger, "info", captured_info.append)
        monkeypatch.setattr(server_security.logger, "warning", captured_warning.append)

        server_security.log_startup_security_warnings()

        assert any("Auth mode:" in message for message in captured_info)
        assert any("test warning" in message for message in captured_warning)

    def test_log_startup_security_warnings_falls_back_to_warning(self, monkeypatch):
        captured_warning = []

        monkeypatch.setattr(
            server_security,
            "get_auth_mode_summary",
            lambda: "Auth mode: anonymous mode; 0 enabled token(s); required=False.",
        )
        monkeypatch.setattr(server_security, "collect_startup_security_warnings", lambda: ["test warning"])
        monkeypatch.setattr(server_security.logger, "isEnabledFor", lambda level: False)
        monkeypatch.setattr(server_security.logger, "warning", captured_warning.append)

        server_security.log_startup_security_warnings()

        assert any("anonymous mode" in message for message in captured_warning)
        assert any("test warning" in message for message in captured_warning)
