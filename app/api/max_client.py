from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class MaxAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class MaxClient:
    """Прямые запросы к platform-api2.max.ru через httpx."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.max_api_base.rstrip("/"),
            timeout=httpx.Timeout(15.0),
            headers=self._headers(),
            verify=settings.max_ssl_verify,
        )

    def _headers(self) -> dict[str, str]:
        token = self.settings.max_bot_token.strip()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        return headers

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        if not self.settings.max_bot_token.strip():
            raise MaxAPIError("MAX_BOT_TOKEN пустой — положи токен в .env")
        response = await self._client.request(method, path, params=params, json=json)
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.status_code >= 400:
            raise MaxAPIError(
                f"MAX API {method} {path} -> {response.status_code}",
                status_code=response.status_code,
                body=payload,
            )
        return payload

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def patch_me(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", "/me", json=body)

    async def send_message(
        self,
        *,
        text: str,
        user_id: int | None = None,
        chat_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        if user_id is None and chat_id is None:
            raise ValueError("Нужен user_id или chat_id")
        params: dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = user_id
        if chat_id is not None:
            params["chat_id"] = chat_id
        body: dict[str, Any] = {"text": text, "notify": notify}
        if attachments is not None:
            body["attachments"] = attachments
        return await self._request("POST", "/messages", params=params, json=body)

    async def answer_callback(
        self,
        callback_id: str,
        *,
        notification: str | None = None,
        message: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        if message:
            body["message"] = message
        return await self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json=body,
        )

    async def get_subscriptions(self) -> dict[str, Any]:
        return await self._request("GET", "/subscriptions")

    async def subscribe_webhook(self) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/subscriptions",
            json={
                "url": self.settings.webhook_url,
                "update_types": self.settings.update_type_list,
                "secret": self.settings.webhook_secret,
            },
        )

    async def unsubscribe_webhook(self, url: str | None = None) -> dict[str, Any]:
        return await self._request(
            "DELETE",
            "/subscriptions",
            params={"url": url or self.settings.webhook_url},
        )

    def subscription_matches(self, payload: dict[str, Any]) -> bool:
        wanted = self.settings.webhook_url.rstrip("/")
        for item in iter_subscriptions(payload):
            url = str(item.get("url") or "").rstrip("/")
            if url == wanted:
                types = set(item.get("update_types") or [])
                needed = set(self.settings.update_type_list)
                return needed.issubset(types) if types else True
        return False

    async def ensure_webhook(self) -> dict[str, Any]:
        current = await self.get_subscriptions()
        if self.subscription_matches(current):
            return {"ok": True, "action": "already_subscribed", "subscriptions": current}
        created = await self.subscribe_webhook()
        return {"ok": True, "action": "subscribed", "result": created, "subscriptions": current}


def iter_subscriptions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("subscriptions")
    if raw is None:
        raw = payload.get("webhooks")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []
