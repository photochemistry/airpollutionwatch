"""Discord webhook による運用通知。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import httpx

from airpollutionwatch.monitoring_links import format_target_datetime_link
from auth_tokens import AUTH_INFO

ALERT_DISCORD_WEBHOOK_ENV = "ALERT_DISCORD_WEBHOOK_URL"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
REVISION_DISCORD_WEBHOOK_ENV = "REVISION_DISCORD_WEBHOOK_URL"
REVISION_DIFF_LIMIT = 10


@dataclass(frozen=True)
class RevisionDiff:
    station_code: int
    field: str
    old_value: float | None
    new_value: float | None


def _auth_value(name: str) -> str:
    raw = AUTH_INFO.get(name)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def alert_webhook_url() -> str:
    env_alert = os.environ.get(ALERT_DISCORD_WEBHOOK_ENV, "").strip()
    env_legacy = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip()
    if env_alert or env_legacy:
        return env_alert or env_legacy
    auth_alert = _auth_value(ALERT_DISCORD_WEBHOOK_ENV)
    auth_legacy = _auth_value(DISCORD_WEBHOOK_ENV)
    return auth_alert or auth_legacy


def revision_webhook_url() -> str:
    """verify 差分通知（REVISION）専用 webhook。収集失敗アラートとは別。"""
    env_revision = os.environ.get(REVISION_DISCORD_WEBHOOK_ENV, "").strip()
    if env_revision:
        return env_revision
    return _auth_value(REVISION_DISCORD_WEBHOOK_ENV)


def _format_diff_value(value: float | None) -> str:
    if value is None:
        return "—"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):g}"


def build_alert_text(
    *,
    prefecture: str,
    target_iso: str,
    since_iso: str,
    error: str,
    failure_hours: int,
    alert_kind: Literal["failure", "no_data"],
) -> str:
    if alert_kind == "no_data":
        headline = "【ALERT】airpollutionwatch 測定データが取得できていません（空欄）"
        kind_line = "- 状態: 収集処理は動いているが、当該時刻の測定値が DB にありません"
    else:
        headline = "【ALERT】airpollutionwatch 収集失敗が継続しています"
        kind_line = "- 状態: 収集失敗（status=failed）"

    return (
        f"{headline}\n"
        f"- 都道府県: {prefecture}\n"
        f"- 対象時刻: {target_iso}\n"
        f"{kind_line}\n"
        f"- 初回検知: {since_iso}\n"
        f"- 継続時間: {failure_hours}時間以上\n"
        f"- 代表メッセージ: {error}"
    )


def build_revision_text(
    *,
    prefecture: str,
    target_iso: str,
    changed_count: int,
    diffs: list[RevisionDiff],
    applied_to_db: bool = False,
) -> str:
    time_link = format_target_datetime_link(prefecture, target_iso)
    lines = [
        "【REVISION】airpollutionwatch 測定値の変更を検出しました",
        f"- 都道府県: {prefecture}",
        f"- 対象時刻: {time_link}",
        f"- 変更件数: {changed_count}",
    ]
    if applied_to_db:
        lines.append("- DB: 反映済み（verify UPSERT）")
    lines.append("- 差分（最大10件）:")
    for diff in diffs[:REVISION_DIFF_LIMIT]:
        lines.append(
            f"  局 {diff.station_code} {diff.field}: "
            f"{_format_diff_value(diff.old_value)} → "
            f"{_format_diff_value(diff.new_value)}"
        )
    if changed_count > REVISION_DIFF_LIMIT:
        lines.append(f"  … 他 {changed_count - REVISION_DIFF_LIMIT} 件")
    return "\n".join(lines)


def send_alert(
    *,
    prefecture: str,
    target_iso: str,
    since_iso: str,
    error: str,
    failure_hours: int,
    alert_kind: Literal["failure", "no_data"],
) -> None:
    webhook = alert_webhook_url()
    if not webhook:
        return

    text = build_alert_text(
        prefecture=prefecture,
        target_iso=target_iso,
        since_iso=since_iso,
        error=error,
        failure_hours=failure_hours,
        alert_kind=alert_kind,
    )
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(webhook, json={"content": text})
        resp.raise_for_status()


def send_revision_alert(
    *,
    prefecture: str,
    target_iso: str,
    changed_count: int,
    diffs: list[RevisionDiff],
    applied_to_db: bool = False,
) -> None:
    webhook = revision_webhook_url()
    if not webhook:
        return

    text = build_revision_text(
        prefecture=prefecture,
        target_iso=target_iso,
        changed_count=changed_count,
        diffs=diffs,
        applied_to_db=applied_to_db,
    )
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(webhook, json={"content": text})
        resp.raise_for_status()
