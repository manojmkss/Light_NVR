"""An alert survives a network blip: each channel is retried, and a failed
alert never uses up the cooldown that would silence the next one."""

import asyncio
from types import SimpleNamespace

from app.services import alerts


def _setup(monkeypatch, telegram_results):
    calls = []
    settings = SimpleNamespace(motion_alerts_enabled=True, motion_alert_cooldown_seconds=600, smtp_host="",
                               alert_email_to="", telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="c",
                               whatsapp_enabled=False)

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a):
            return settings

    async def decide(event):
        return SimpleNamespace(silent=False, cooldown_s=None, urgent=False, webhook_url=None)

    async def send(token, chat, caption):
        calls.append(caption)
        ok = telegram_results.pop(0) if telegram_results else False
        return ok, "ok" if ok else "network unreachable"

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(alerts, "AsyncSessionLocal", lambda: DB())
    monkeypatch.setattr("app.services.notify_policy.decide", decide)
    monkeypatch.setattr(alerts, "send_telegram_message", send)
    monkeypatch.setattr(alerts, "_event_link", nothing)
    monkeypatch.setattr(alerts, "_maybe_send_push", nothing)
    monkeypatch.setattr(alerts, "RETRY_DELAYS", (0.01, 0.01))
    monkeypatch.setattr(alerts, "_last_alert_at", {})
    return calls


def _event(i):
    return SimpleNamespace(id=i, type="motion", camera_id=2, message="Person at the gate", snapshot_path=None,
                           priority=0)


def test_a_failed_send_is_retried_until_it_goes_through(monkeypatch):
    calls = _setup(monkeypatch, [False, False, True])
    asyncio.run(alerts.maybe_send_alert(_event(1)))
    assert len(calls) == 3  # two failures, delivered on the third try
    assert ("motion", 2) in alerts._last_alert_at  # delivered: the cooldown applies


def test_an_alert_that_could_not_be_delivered_does_not_silence_the_next(monkeypatch):
    calls = _setup(monkeypatch, [False, False, False, True])
    asyncio.run(alerts.maybe_send_alert(_event(1)))
    assert len(calls) == 3 and ("motion", 2) not in alerts._last_alert_at
    asyncio.run(alerts.maybe_send_alert(_event(2)))  # the network is back a minute later
    assert len(calls) == 4 and ("motion", 2) in alerts._last_alert_at
    asyncio.run(alerts.maybe_send_alert(_event(3)))  # and now the cooldown holds as usual
    assert len(calls) == 4
