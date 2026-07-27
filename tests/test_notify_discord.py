"""notify_discord のテスト。"""
from __future__ import annotations

import notify_discord as discord


def test_revision_alert_includes_time_link(monkeypatch):
    captured: list[str] = []

    def fake_post(self, url, *, json=None, **kwargs):
        captured.append(json["content"])

        class Resp:
            def raise_for_status(self):
                return None

        return Resp()

    monkeypatch.setenv("REVISION_DISCORD_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setattr(discord.httpx.Client, "__enter__", lambda self: self)
    monkeypatch.setattr(discord.httpx.Client, "__exit__", lambda *a: None)
    monkeypatch.setattr(discord.httpx.Client, "post", fake_post)

    discord.send_revision_alert(
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

    assert captured
    assert "[2026-06-25 13:00 JST](" in captured[0]
    assert "sokuhou.php" in captured[0]


def test_revision_webhook_separate_from_alert(monkeypatch):
    monkeypatch.setenv("REVISION_DISCORD_WEBHOOK_URL", "https://example.com/revision")
    monkeypatch.setenv("ALERT_DISCORD_WEBHOOK_URL", "https://example.com/alert")
    assert discord.revision_webhook_url() == "https://example.com/revision"
    assert discord.alert_webhook_url() == "https://example.com/alert"
