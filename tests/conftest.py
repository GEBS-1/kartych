from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        max_bot_token="test-token",
        max_bot_username="cupcard_bot",
        webhook_secret="secret123",
        public_base_url="https://example.test",
        subscribe_on_startup=False,
        watchdog_interval_seconds=15,
        admin_max_user_ids="99001",
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        app.state.max_client.send_message = AsyncMock(return_value={"ok": True})
        app.state.max_client.answer_callback = AsyncMock(return_value={"ok": True})
        yield test_client
