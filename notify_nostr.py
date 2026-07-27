"""Nostr 公開フィード（kind 1）による運用通知。"""
from __future__ import annotations

import json
import os
from typing import Literal

from nostr.event import Event, EventKind
from nostr.key import PrivateKey
from websocket import create_connection

import notify_discord as discord
from auth_tokens import AUTH_INFO

ALERT_NOSTR_NSEC_ENV = "ALERT_NOSTR_NSEC"
ALERT_NOSTR_RELAYS_ENV = "ALERT_NOSTR_RELAYS"
REVISION_NOSTR_NSEC_ENV = "REVISION_NOSTR_NSEC"
REVISION_NOSTR_RELAYS_ENV = "REVISION_NOSTR_RELAYS"
_RELAY_TIMEOUT_SEC = 12


class NostrPublishError(Exception):
    """いずれのリレーにも投稿できなかった。"""


def _auth_value(name: str) -> str:
    raw = AUTH_INFO.get(name)
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def _relay_urls(name: str) -> list[str]:
    env_raw = os.environ.get(name, "").strip()
    if env_raw:
        if env_raw.startswith("["):
            parsed = json.loads(env_raw)
            if not isinstance(parsed, list):
                raise ValueError(f"{name} は URL のリストである必要があります")
            return [str(url).strip() for url in parsed if str(url).strip()]
        return [part.strip() for part in env_raw.split(",") if part.strip()]

    raw = AUTH_INFO.get(name)
    if isinstance(raw, list):
        return [str(url).strip() for url in raw if str(url).strip()]
    if isinstance(raw, str) and raw.strip():
        if raw.strip().startswith("["):
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError(f"{name} は URL のリストである必要があります")
            return [str(url).strip() for url in parsed if str(url).strip()]
        return [part.strip() for part in raw.split(",") if part.strip()]
    return []


def alert_nsec() -> str:
    env = os.environ.get(ALERT_NOSTR_NSEC_ENV, "").strip()
    if env:
        return env
    return _auth_value(ALERT_NOSTR_NSEC_ENV)


def revision_nsec() -> str:
    env = os.environ.get(REVISION_NOSTR_NSEC_ENV, "").strip()
    if env:
        return env
    return _auth_value(REVISION_NOSTR_NSEC_ENV)


def alert_relays() -> list[str]:
    return _relay_urls(ALERT_NOSTR_RELAYS_ENV)


def revision_relays() -> list[str]:
    return _relay_urls(REVISION_NOSTR_RELAYS_ENV)


def _load_private_key(nsec: str) -> PrivateKey:
    return PrivateKey.from_nsec(nsec.strip())


def _event_wire(event: Event) -> str:
    return json.dumps(
        [
            "EVENT",
            {
                "id": event.id,
                "pubkey": event.public_key,
                "created_at": event.created_at,
                "kind": event.kind,
                "tags": event.tags,
                "content": event.content,
                "sig": event.signature,
            },
        ],
        separators=(",", ":"),
    )


def publish_note(*, nsec: str, relays: list[str], content: str) -> str:
    """kind 1 を全リレーへ投稿する。1つ以上 OK なら成功とし event id を返す。"""
    if not nsec or not relays:
        raise NostrPublishError("nsec または relays が未設定です")

    pk = _load_private_key(nsec)
    event = Event(content=content, public_key=pk.public_key.hex(), kind=EventKind.TEXT_NOTE)
    pk.sign_event(event)
    wire = _event_wire(event)

    errors: list[str] = []
    ok_count = 0
    for url in relays:
        try:
            ws = create_connection(url, timeout=_RELAY_TIMEOUT_SEC)
            try:
                ws.send(wire)
                resp = ws.recv()
            finally:
                ws.close()
            if isinstance(resp, bytes):
                resp = resp.decode("utf-8", errors="replace")
            if resp.startswith('["OK"'):
                ok_count += 1
            else:
                errors.append(f"{url}: {resp[:160]}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")

    if ok_count:
        return event.id
    raise NostrPublishError("; ".join(errors))


def send_alert(
    *,
    prefecture: str,
    target_iso: str,
    since_iso: str,
    error: str,
    failure_hours: int,
    alert_kind: Literal["failure", "no_data"],
) -> str:
    nsec = alert_nsec()
    relays = alert_relays()
    if not nsec or not relays:
        raise NostrPublishError("ALERT Nostr が未設定です")

    content = discord.build_alert_text(
        prefecture=prefecture,
        target_iso=target_iso,
        since_iso=since_iso,
        error=error,
        failure_hours=failure_hours,
        alert_kind=alert_kind,
    )
    return publish_note(nsec=nsec, relays=relays, content=content)


def send_revision_alert(
    *,
    prefecture: str,
    target_iso: str,
    changed_count: int,
    diffs: list[discord.RevisionDiff],
    applied_to_db: bool = False,
) -> str:
    nsec = revision_nsec()
    relays = revision_relays()
    if not nsec or not relays:
        raise NostrPublishError("REVISION Nostr が未設定です")

    content = discord.build_revision_text(
        prefecture=prefecture,
        target_iso=target_iso,
        changed_count=changed_count,
        diffs=diffs,
        applied_to_db=applied_to_db,
    )
    return publish_note(nsec=nsec, relays=relays, content=content)
