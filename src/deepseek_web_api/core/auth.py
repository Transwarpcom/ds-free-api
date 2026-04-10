"""Authentication for single account mode."""

import logging
import threading

from curl_cffi import requests

from .config import (
    DEEPSEEK_LOGIN_URL,
    clear_persisted_account_token,
    get_account_config,
    get_base_headers,
    get_default_impersonate,
    get_persisted_account_token,
    persist_account_token,
)

logger = logging.getLogger(__name__)

# Global single account
_account = None
_token_lock = threading.Lock()  # Protect _account check-and-set


def init_single_account():
    """Initialize single account from config (lazy, no login)."""
    global _account
    if _account is None:
        _account = get_account_config()
    if not _account:
        raise ValueError("No account configured")


def login() -> str:
    """Login and get new token, then save to the configured persistence layer."""
    global _account
    email = _account.get("email", "").strip()
    mobile = _account.get("mobile", "").strip()
    password = _account.get("password", "").strip()

    if not password or (not email and not mobile):
        logger.error("[login] Missing email/mobile or password")
        raise ValueError("Account missing required login info (email or mobile and password required)")

    payload = {
        "password": password,
        "device_id": "deepseek_to_api",
        "os": "android",
    }
    if email:
        payload["email"] = email
    else:
        payload["mobile"] = mobile
        payload["area_code"] = _account.get("area_code")

    logger.info("[login] Attempting login...")
    resp = requests.post(
        DEEPSEEK_LOGIN_URL,
        headers=get_base_headers(),
        json=payload,
        impersonate=get_default_impersonate(),
    )
    data = resp.json()
    resp.close()

    if data.get("data") is None or data["data"].get("biz_data") is None:
        logger.error("[login] Invalid response format from DeepSeek")
        raise ValueError("Login failed: invalid response format")

    new_token = data["data"]["biz_data"]["user"].get("token")
    if not new_token:
        logger.error("[login] Missing token in response")
        raise ValueError("Login failed: missing token")

    _account["token"] = new_token
    _save_token(new_token)

    logger.info("[login] Login successful, token obtained and saved")
    return new_token


def _save_token(token: str):
    """Persist token without forcing secrets back into config.toml."""
    try:
        persist_account_token(token)
        logger.debug("[login] Token persisted")
    except Exception as e:
        logger.warning(f"[login] Failed to save token: {e}")


def invalidate_token():
    """Invalidate current token, forcing refresh on next get_token()."""
    global _account
    if _account:
        _account.pop("token", None)
        logger.debug("[invalidate_token] Token invalidated in memory")

    try:
        clear_persisted_account_token()
        logger.debug("[invalidate_token] Token invalidated in persistence layer")
    except Exception as e:
        logger.warning(f"[invalidate_token] Failed to clear persisted token: {e}")


def get_token() -> str:
    """Get current token, login if needed (lazy initialization)."""
    global _account

    if _account and _account.get("token"):
        return _account["token"]

    with _token_lock:
        if _account and _account.get("token"):
            return _account["token"]

        if _account is None:
            _account = get_account_config()
            if not _account:
                raise ValueError("No account configured")

        persisted_token = get_persisted_account_token()
        if persisted_token:
            _account["token"] = persisted_token
            logger.debug("[get_token] Loaded token from persistence layer")
            return persisted_token

        return login()


def get_auth_headers() -> dict:
    """Get headers with authorization."""
    return {**get_base_headers(), "authorization": f"Bearer {get_token()}"}
