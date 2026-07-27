"""運用通知のマルチチャネル窓口。

チャネルごとの実装は notify_*.py に分離する。
"""
from __future__ import annotations

import logging
from typing import Literal, Protocol

import httpx

import notify_discord as discord
import notify_nostr as nostr

logger = logging.getLogger(__name__)


class _RevisionDiffLike(Protocol):
    station_code: int
    field: str
    old_value: float | None
    new_value: float | None


def _discord_diffs(diffs: list[_RevisionDiffLike]) -> list[discord.RevisionDiff]:
    return [
        discord.RevisionDiff(
            station_code=d.station_code,
            field=d.field,
            old_value=d.old_value,
            new_value=d.new_value,
        )
        for d in diffs
    ]


def configured_alert_channels() -> list[str]:
    """設定済みの ingest アラートチャネル名（discord / nostr）。"""
    channels: list[str] = []
    if discord.alert_webhook_url():
        channels.append("discord")
    if nostr.alert_nsec() and nostr.alert_relays():
        channels.append("nostr")
    return channels


def notify_ingest_alert(
    *,
    prefecture: str,
    target_iso: str,
    since_iso: str,
    error: str,
    failure_hours: int,
    alert_kind: Literal["failure", "no_data"],
    skip_channels: frozenset[str] | None = None,
) -> list[str]:
    """収集失敗・空データアラートを有効なチャネルへ送る。成功したチャネル名のリストを返す。"""
    skip = skip_channels or frozenset()
    sent: list[str] = []
    if "discord" not in skip and discord.alert_webhook_url():
        try:
            discord.send_alert(
                prefecture=prefecture,
                target_iso=target_iso,
                since_iso=since_iso,
                error=error,
                failure_hours=failure_hours,
                alert_kind=alert_kind,
            )
            sent.append("discord")
        except httpx.HTTPError as e:
            logger.warning(
                "failed to send discord ingest alert: pref=%s target=%s err=%s",
                prefecture,
                target_iso,
                e,
            )

    if "nostr" not in skip and nostr.alert_nsec() and nostr.alert_relays():
        try:
            nostr.send_alert(
                prefecture=prefecture,
                target_iso=target_iso,
                since_iso=since_iso,
                error=error,
                failure_hours=failure_hours,
                alert_kind=alert_kind,
            )
            sent.append("nostr")
        except nostr.NostrPublishError as e:
            logger.warning(
                "failed to send nostr ingest alert: pref=%s target=%s err=%s",
                prefecture,
                target_iso,
                e,
            )

    return sent


def notify_revision(
    *,
    prefecture: str,
    target_iso: str,
    changed_count: int,
    diffs: list[_RevisionDiffLike],
    applied_to_db: bool = False,
) -> list[str]:
    """verify 差分を有効なチャネルへ送る。成功したチャネル名のリストを返す。"""
    sent: list[str] = []
    discord_diffs = _discord_diffs(diffs)

    if discord.revision_webhook_url():
        try:
            discord.send_revision_alert(
                prefecture=prefecture,
                target_iso=target_iso,
                changed_count=changed_count,
                diffs=discord_diffs,
                applied_to_db=applied_to_db,
            )
            sent.append("discord")
        except httpx.HTTPError as e:
            logger.warning(
                "failed to send discord revision alert: pref=%s target=%s err=%s",
                prefecture,
                target_iso,
                e,
            )

    if nostr.revision_nsec() and nostr.revision_relays():
        try:
            nostr.send_revision_alert(
                prefecture=prefecture,
                target_iso=target_iso,
                changed_count=changed_count,
                diffs=discord_diffs,
                applied_to_db=applied_to_db,
            )
            sent.append("nostr")
        except nostr.NostrPublishError as e:
            logger.warning(
                "failed to send nostr revision alert: pref=%s target=%s err=%s",
                prefecture,
                target_iso,
                e,
            )

    return sent
