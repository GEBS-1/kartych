from __future__ import annotations

from typing import Any

import httpx


class TelegramAPIError(RuntimeError):
    def __init__(self, message: str, *, body: Any = None) -> None:
        super().__init__(message)
        self.body = body


class TelegramClient:
    def __init__(self, token: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.token = (token or "").strip()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, **payload: Any) -> Any:
        if not self.token:
            raise TelegramAPIError("Нет токена Telegram-бота")
        response = await self._client.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=payload,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramAPIError(f"Telegram {method} не JSON", body=response.text) from exc
        if not body.get("ok"):
            raise TelegramAPIError(
                str(body.get("description") or f"Telegram {method} ошибка"),
                body=body,
            )
        return body.get("result")

    async def get_me(self) -> dict[str, Any]:
        result = await self._call("getMe")
        return result if isinstance(result, dict) else {}

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", **payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        return await self._call("answerCallbackQuery", **payload)

    async def set_webhook(self, url: str, secret_token: str) -> Any:
        return await self._call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            allowed_updates=["message", "callback_query"],
        )

    async def delete_webhook(self) -> Any:
        return await self._call("deleteWebhook")
