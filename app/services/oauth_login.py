"""ChatGPT / Grok 로그인 연결.

이 앱은 원래 API 키를 직접 붙여 넣는다. 임원이 키를 만지지 않고도
ChatGPT 계정과 Grok 계정을 연결할 수 있게, 로컬 로그인 상태를 읽어 모델 호출에 쓴다.

- ChatGPT: 이 Mac의 Codex/ChatGPT 로그인(~/.codex/auth.json)을 사용한다.
- Grok: 브라우저에서 xAI 콘솔을 연 뒤, 사용자가 붙여 넣은 API 키를 저장한다.
  xAI는 일반 앱용 공개 OAuth 클라이언트 ID를 제공하지 않아, 콘솔 로그인 후 키를 받는 방식이 공식 경로다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHATGPT_AUTH_PATH = Path.home() / ".codex" / "auth.json"
GROK_CONSOLE_URL = "https://console.x.ai/"
CHATGPT_ACCOUNT_URL = "https://chatgpt.com/"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def chatgpt_status(auth_path: Path | None = None) -> dict[str, Any]:
    path = auth_path or CHATGPT_AUTH_PATH
    data = _read_json(path)
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access = str(tokens.get("access_token") or "").strip()
    account_id = str(tokens.get("account_id") or "").strip()
    mode = str(data.get("auth_mode") or "").strip()
    connected = bool(access) and mode in {"chatgpt", "ChatGPT", ""} or bool(access)
    return {
        "provider": "openai",
        "connected": bool(access),
        "mode": mode or "chatgpt",
        "account_id": account_id,
        "auth_path": str(path),
        "last_refresh": str(data.get("last_refresh") or ""),
        "access_token": access,
    }


def grok_status(app_config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = app_config or {}
    api_key = str(config.get("grok_api_key") or "").strip()
    return {
        "provider": "grok",
        "connected": bool(api_key),
        "console_url": GROK_CONSOLE_URL,
        "has_key": bool(api_key),
    }


def resolve_openai_credentials(app_config: dict[str, Any], auth_path: Path | None = None) -> tuple[str, str]:
    """설정 키를 우선하고, 비어 있으면 ChatGPT 로컬 로그인을 사용한다."""
    api_key = str(app_config.get("openai_api_key") or "").strip()
    base_url = str(app_config.get("openai_base_url") or "").strip()
    if api_key:
        return api_key, base_url
    status = chatgpt_status(auth_path=auth_path)
    if status["connected"] and status["access_token"]:
        return status["access_token"], base_url
    return "", base_url


def mask_account(account_id: str) -> str:
    value = str(account_id or "").strip()
    if len(value) <= 8:
        return value
    return f"{value[:4]}...{value[-4:]}"
