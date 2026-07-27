"""notify_nostr のテスト。"""
from __future__ import annotations

import notify_discord as discord
import notify_nostr as nostr


def test_build_alert_text_shared_with_discord():
    text = discord.build_alert_text(
        prefecture="nara",
        target_iso="2026-06-02T09:00:00+09:00",
        since_iso="2026-06-02T06:00:00+09:00",
        error="empty",
        failure_hours=3,
        alert_kind="no_data",
    )
    assert "【ALERT】" in text
    assert "nara" in text


def test_publish_note_success(monkeypatch):
    sent: list[str] = []

    class FakeWS:
        def send(self, wire):
            sent.append(wire)

        def recv(self):
            return '["OK","event-id",true,""]'

        def close(self):
            return None

    class FakePub:
        def hex(self):
            return "a" * 64

    class FakePK:
        public_key = FakePub()

        def sign_event(self, event):
            event.signature = "b" * 128
            event.id = "c" * 64

    monkeypatch.setattr(nostr, "create_connection", lambda url, timeout: FakeWS())
    monkeypatch.setattr(nostr, "_load_private_key", lambda nsec: FakePK())

    event_id = nostr.publish_note(
        nsec="nsec1dummy",
        relays=["wss://nos.lol"],
        content="hello",
    )
    assert event_id
    assert sent
    assert '"kind":1' in sent[0]


def test_send_revision_uses_revision_credentials(monkeypatch):
    captured: dict = {}

    def fake_publish(*, nsec, relays, content):
        captured["nsec"] = nsec
        captured["relays"] = relays
        captured["content"] = content
        return "event-id"

    monkeypatch.setenv("REVISION_NOSTR_NSEC", "nsec1test")
    monkeypatch.setenv("REVISION_NOSTR_RELAYS", '["wss://nos.lol"]')
    monkeypatch.setattr(nostr, "publish_note", fake_publish)

    nostr.send_revision_alert(
        prefecture="gifu",
        target_iso="2026-06-25T13:00:00+09:00",
        changed_count=1,
        diffs=[
            discord.RevisionDiff(
                station_code=29201020,
                field="PM25",
                old_value=10.0,
                new_value=12.0,
            )
        ],
    )

    assert captured["nsec"] == "nsec1test"
    assert captured["relays"] == ["wss://nos.lol"]
    assert "【REVISION】" in captured["content"]
    assert "sokuhou.php" in captured["content"]


def test_publish_note_fans_out_to_all_relays(monkeypatch):
    sent_urls: list[str] = []

    class FakeWS:
        def send(self, wire):
            return None

        def recv(self):
            return '["OK","event-id",true,""]'

        def close(self):
            return None

    class FakePub:
        def hex(self):
            return "a" * 64

    class FakePK:
        public_key = FakePub()

        def sign_event(self, event):
            event.signature = "b" * 128
            event.id = "c" * 64

    def fake_connection(url, timeout):
        sent_urls.append(url)
        return FakeWS()

    monkeypatch.setattr(nostr, "create_connection", fake_connection)
    monkeypatch.setattr(nostr, "_load_private_key", lambda nsec: FakePK())

    event_id = nostr.publish_note(
        nsec="nsec1dummy",
        relays=["wss://a.example", "wss://b.example"],
        content="hello",
    )
    assert event_id
    assert sent_urls == ["wss://a.example", "wss://b.example"]


def test_notify_ingest_alert_sends_both_channels(monkeypatch):
    import notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        notify_mod.discord,
        "alert_webhook_url",
        lambda: "https://example.com/discord",
    )
    monkeypatch.setattr(
        notify_mod.discord,
        "send_alert",
        lambda **kwargs: calls.append("discord"),
    )
    monkeypatch.setattr(notify_mod.nostr, "alert_nsec", lambda: "nsec1test")
    monkeypatch.setattr(notify_mod.nostr, "alert_relays", lambda: ["wss://nos.lol"])
    monkeypatch.setattr(
        notify_mod.nostr,
        "send_alert",
        lambda **kwargs: calls.append("nostr"),
    )

    channels = notify_mod.notify_ingest_alert(
        prefecture="nara",
        target_iso="2026-06-02T09:00:00+09:00",
        since_iso="2026-06-02T06:00:00+09:00",
        error="empty",
        failure_hours=3,
        alert_kind="no_data",
    )

    assert channels == ["discord", "nostr"]
    assert calls == ["discord", "nostr"]


def test_notify_revision_sends_both_channels(monkeypatch):
    import notify as notify_mod

    calls: list[str] = []
    monkeypatch.setattr(
        discord,
        "revision_webhook_url",
        lambda: "https://example.com/discord",
    )
    monkeypatch.setattr(discord, "send_revision_alert", lambda **kwargs: calls.append("discord"))
    monkeypatch.setattr(nostr, "revision_nsec", lambda: "nsec1test")
    monkeypatch.setattr(nostr, "revision_relays", lambda: ["wss://nos.lol"])
    monkeypatch.setattr(
        nostr,
        "send_revision_alert",
        lambda **kwargs: calls.append("nostr") or "id",
    )

    channels = notify_mod.notify_revision(
        prefecture="nara",
        target_iso="2026-06-02T10:00:00+09:00",
        changed_count=1,
        diffs=[
            discord.RevisionDiff(
                station_code=1,
                field="PM25",
                old_value=1.0,
                new_value=2.0,
            )
        ],
    )

    assert channels == ["discord", "nostr"]
    assert calls == ["discord", "nostr"]
