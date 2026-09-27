from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

PREFIX = "c_"
TTL = 60 * 10


def tickets_path_for(database_url: str) -> Path | None:
    url = (database_url or "").strip()
    if "sqlite" not in url or ":memory:" in url:
        return None
    if "///" not in url:
        return None
    raw = url.split("///", 1)[1]
    if not raw or raw == ":memory:":
        return None
    db_path = Path(raw)
    if not db_path.name or db_path.name == ":memory:":
        return None
    return db_path.with_name("login_tickets.json")


class LoginTickets:
    def __init__(self, path: Path | str | None = None) -> None:
        self._path: Path | None = Path(path) if path else None
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load()

    def configure(self, path: Path | str | None) -> None:
        with self._lock:
            self._path = Path(path) if path else None
            self._load_unlocked()

    def create(self, *, role: str, invite: str = "", promo: str = "") -> str:
        with self._lock:
            self._prune_unlocked()
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
            self._save_unlocked()
            return token

    def payload(self, token: str) -> str:
        return f"{PREFIX}{token}"

    def parse(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if text.startswith(PREFIX) and len(text) == len(PREFIX) + 16:
            return text[len(PREFIX) :]
        return None

    def complete(self, token: str, user_id: int) -> bool:
        with self._lock:
            item = self._items.get(token)
            if item is None or item["status"] != "pending":
                return False
            if time.time() - float(item["created"]) > TTL:
                return False
            item["status"] = "ok"
            item["user_id"] = user_id
            self._save_unlocked()
            return True

    def peek(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(token)
            if item is None or time.time() - float(item["created"]) > TTL:
                return None
            return dict(item)

    def consume(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(token)
            if item is None or time.time() - float(item["created"]) > TTL:
                return None
            if item["status"] != "ok" or item["user_id"] is None:
                return None
            item["status"] = "used"
            snapshot = dict(item)
            self._save_unlocked()
            return snapshot

    def status(self, token: str) -> str:
        with self._lock:
            item = self._items.get(token)
            if item is None:
                return "missing"
            if time.time() - float(item["created"]) > TTL:
                return "expired"
            return str(item["status"])

    def _prune_unlocked(self) -> None:
        now = time.time()
        dead = [key for key, item in self._items.items() if now - float(item["created"]) > TTL]
        for key in dead:
            self._items.pop(key, None)

    def _load(self) -> None:
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._items = {}
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        now = time.time()
        for token, item in raw.items():
            if not isinstance(token, str) or not isinstance(item, dict):
                continue
            try:
                created = float(item.get("created") or 0)
            except (TypeError, ValueError):
                continue
            if now - created > TTL:
                continue
            self._items[token] = {
                "status": str(item.get("status") or "pending"),
                "user_id": item.get("user_id"),
                "role": str(item.get("role") or "client"),
                "invite": str(item.get("invite") or ""),
                "promo": str(item.get("promo") or ""),
                "created": created,
            }

    def _save_unlocked(self) -> None:
        if self._path is None:
            return
        self._prune_unlocked()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._items, ensure_ascii=False)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            return


tickets = LoginTickets()
