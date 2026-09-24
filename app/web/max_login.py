from __future__ import annotations

import secrets
import time
from typing import Any

PREFIX = "c_"
TTL = 60 * 10


class LoginTickets:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}

    def create(self, *, role: str, invite: str = "", promo: str = "") -> str:
        self._prune()
        token = secrets.token_hex(8)
        clean = invite.strip() if invite and len(invite.strip()) == 16 else ""
        promo_id = promo.strip() if promo and len(promo.strip()) == 16 else ""
        self._items[token] = {
            "status": "pending",
            "user_id": None,
            "role": role if role in {"client", "business"} else "client",
            "invite": clean,
            "promo": promo_id,
            "created": time.time(),
        }
        return token

    def payload(self, token: str) -> str:
        return f"{PREFIX}{token}"

    def parse(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if text.startswith(PREFIX) and len(text) == len(PREFIX) + 16:
            return text[len(PREFIX) :]
        return None

    def complete(self, token: str, user_id: int) -> bool:
        item = self._items.get(token)
        if item is None or item["status"] != "pending":
            return False
        if time.time() - item["created"] > TTL:
            return False
        item["status"] = "ok"
        item["user_id"] = user_id
        return True

    def peek(self, token: str) -> dict[str, Any] | None:
        item = self._items.get(token)
        if item is None or time.time() - item["created"] > TTL:
            return None
        return item

    def consume(self, token: str) -> dict[str, Any] | None:
        item = self.peek(token)
        if item is None or item["status"] != "ok" or item["user_id"] is None:
            return None
        item["status"] = "used"
        return dict(item)

    def status(self, token: str) -> str:
        item = self._items.get(token)
        if item is None:
            return "missing"
        if time.time() - item["created"] > TTL:
            return "expired"
        return str(item["status"])

    def _prune(self) -> None:
        now = time.time()
        dead = [key for key, item in self._items.items() if now - item["created"] > TTL]
        for key in dead:
            self._items.pop(key, None)


tickets = LoginTickets()
