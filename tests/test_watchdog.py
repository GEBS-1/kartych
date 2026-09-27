from types import SimpleNamespace

import pytest

from app.api.max_client import MaxClient, extra_subscription_urls, iter_subscriptions
from app.config import Settings
from app.workers.subscription_watchdog import check_subscriptions


def test_iter_and_match() -> None:
    settings = Settings(public_base_url="https://cup.test", webhook_path="/webhook")
    client = MaxClient(settings)
    payload = {
        "subscriptions": [
            {
                "url": "https://cup.test/webhook",
                "update_types": ["bot_started", "message_created", "message_callback", "bot_stopped"],
            }
        ]
    }
    extra = "https://playpen-slashed-lizard.ngrok-free.dev/webhook"
    assert iter_subscriptions(payload)[0]["url"] == "https://cup.test/webhook"
    assert client.subscription_matches(payload) is True
    assert client.subscription_matches({"webhooks": []}) is False
    assert extra_subscription_urls(payload, settings.webhook_url) == []
    assert extra_subscription_urls(
        {"subscriptions": [{"url": settings.webhook_url}, {"url": extra}]},
        settings.webhook_url,
    ) == [extra]


@pytest.mark.asyncio
async def test_watchdog_resubscribes_when_missing() -> None:
    settings = Settings(public_base_url="https://cup.test", webhook_path="/webhook", max_bot_token="x")

    class FakeClient:
        def __init__(self) -> None:
            self.subscribed = False
            self.settings = settings

        def subscription_matches(self, payload: dict) -> bool:
            return False

        async def get_subscriptions(self) -> dict:
            return {"subscriptions": []}

        async def subscribe_webhook(self) -> dict:
            self.subscribed = True
            return {"success": True}

        async def unsubscribe_webhook(self, url: str | None = None) -> dict:
            raise AssertionError("no extras to drop")

    fake = FakeClient()
    app = SimpleNamespace(state=SimpleNamespace(max_client=fake, settings=settings))
    status = await check_subscriptions(app)  # type: ignore[arg-type]
    assert fake.subscribed is True
    assert status["action"] == "resubscribed"
    assert status["ok"] is True


@pytest.mark.asyncio
async def test_watchdog_drops_extra_webhooks() -> None:
    settings = Settings(public_base_url="https://cup.test", webhook_path="/webhook", max_bot_token="x")
    extra = "https://playpen-slashed-lizard.ngrok-free.dev/webhook"

    class FakeClient:
        def __init__(self) -> None:
            self.settings = settings
            self.removed: list[str] = []
            self.subscribed = False

        def subscription_matches(self, payload: dict) -> bool:
            return True

        async def get_subscriptions(self) -> dict:
            return {
                "subscriptions": [
                    {"url": settings.webhook_url},
                    {"url": extra},
                ]
            }

        async def unsubscribe_webhook(self, url: str | None = None) -> dict:
            self.removed.append(url or "")
            return {"success": True}

        async def subscribe_webhook(self) -> dict:
            self.subscribed = True
            return {"success": True}

    fake = FakeClient()
    app = SimpleNamespace(state=SimpleNamespace(max_client=fake, settings=settings))
    status = await check_subscriptions(app)  # type: ignore[arg-type]
    assert fake.removed == [extra]
    assert fake.subscribed is False
    assert status["action"] == "pruned"
    assert status["ok"] is True
    assert status["removed"] == [extra]
