"""
grid/field のバックグラウンド計算とリクエスト合流.

クライアントが接続を切ってもスレッドプール上の計算は完了し、
response_cache へ保存する。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Literal, Optional

from grid.response_cache import get_response_cache, put_response_cache

logger = logging.getLogger(__name__)

FieldJobStatus = Literal["hit", "computed", "pending", "error"]

_WAIT_SECONDS = float(os.getenv("GRID_FIELD_WAIT_SECONDS", "20"))
_MAX_WORKERS = int(os.getenv("GRID_FIELD_WORKERS", "2"))

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="grid-field")
_registry_lock = threading.Lock()
_inflight: dict[str, Future[None]] = {}


def wait_seconds() -> float:
    return _WAIT_SECONDS


def _run_compute(
    cache_key: str,
    compute: Callable[[], bytes],
    data_version_fn: Optional[Callable[[], str | None]],
) -> None:
    try:
        body = compute()
        dv = data_version_fn() if data_version_fn is not None else None
        put_response_cache(cache_key, body, data_version=dv)
        logger.info("grid response cache stored: %s (%d bytes)", cache_key, len(body))
    except Exception:
        logger.exception("grid field background compute failed: %s", cache_key)
        raise


def get_or_compute_field_response(
    cache_key: str,
    compute: Callable[[], bytes],
    *,
    data_version_fn: Optional[Callable[[], str | None]] = None,
    wait_seconds: float | None = None,
) -> tuple[bytes | None, FieldJobStatus, str | None]:
    """
    レスポンス body を返す。戻り値は (body, status, error_detail)。

    status:
      hit       - response_cache から返した
      computed  - このリクエストで計算完了
      pending   - 待機時間内に完了せず（バックグラウンドは継続）
      error     - 計算失敗
    """
    if wait_seconds is None:
        wait_seconds = _WAIT_SECONDS

    cached = get_response_cache(cache_key)
    if cached is not None:
        logger.info("grid response cache hit: %s", cache_key)
        return cached, "hit", None

    future: Future[None] | None = None
    with _registry_lock:
        existing = _inflight.get(cache_key)
        if existing is None or existing.done():
            if existing is not None and existing.done():
                exc = existing.exception()
                if exc is not None:
                    _inflight.pop(cache_key, None)
                    return None, "error", str(exc)
                # 完了直後: キャッシュを再確認
                cached = get_response_cache(cache_key)
                if cached is not None:
                    _inflight.pop(cache_key, None)
                    return cached, "hit", None
            future = _executor.submit(
                _run_compute, cache_key, compute, data_version_fn
            )
            _inflight[cache_key] = future
        else:
            future = existing

    try:
        future.result(timeout=wait_seconds)
    except TimeoutError:
        logger.info(
            "grid field wait timeout (%.1fs); background continues: %s",
            wait_seconds,
            cache_key,
        )
        return (
            None,
            "pending",
            "computation continues in background; retry shortly",
        )
    except Exception as e:
        with _registry_lock:
            _inflight.pop(cache_key, None)
        return None, "error", str(e)

    with _registry_lock:
        _inflight.pop(cache_key, None)

    cached = get_response_cache(cache_key)
    if cached is not None:
        return cached, "computed", None
    return None, "error", "cache missing after compute"
