"""Bearer トークン認証（ingest）。"""
from __future__ import annotations

import logging
import os
import runpy
from pathlib import Path

from fastapi import HTTPException, status

from config import ROOT

logger = logging.getLogger(__name__)

INGEST_TOKEN_ENV = "AIRPOLLUTIONWATCH_INGEST_TOKEN"
INGEST_TOKEN_FILE_ENV = "AIRPOLLUTIONWATCH_INGEST_TOKEN_FILE"


def _load_auth_info() -> dict[str, object]:
    """任意の auth_info.py を読み込む（存在しなければ空）。"""
    path = ROOT / "auth_info.py"
    if not path.is_file():
        return {}
    try:
        data = runpy.run_path(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning("auth_info.py の読み込みに失敗: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


AUTH_INFO = _load_auth_info()


def _auth_value(name: str) -> str:
    raw = AUTH_INFO.get(name)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def resolve_expected_token(token_env: str, token_file_env: str) -> str:
    expected = os.environ.get(token_env, "").strip()
    if not expected:
        expected = _auth_value(token_env)
    if expected:
        return expected
    token_file = os.environ.get(token_file_env, "").strip()
    if not token_file:
        token_file = _auth_value(token_file_env)
    if not token_file:
        return ""
    try:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{token_file_env} の読み取りに失敗しました: {e}",
        ) from e


def verify_ingest_token(authorization: str | None) -> None:
    expected = resolve_expected_token(INGEST_TOKEN_ENV, INGEST_TOKEN_FILE_ENV)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{INGEST_TOKEN_ENV} または {INGEST_TOKEN_FILE_ENV} が未設定です",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization: Bearer <token> が必要です",
        )
    token = authorization[len("Bearer ") :].strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="トークンが不正です",
        )
