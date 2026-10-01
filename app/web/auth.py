from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import unquote

from fastapi import Request, Response

COOKIE = "cup_session"
TTL = 60 * 60 * 24 * 30


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[
        :32
    ]


def site_user_id(name: str) -> int:
    digest = hashlib.sha256(f"cupcard-site:{name.strip().lower()}".encode()).hexdigest()
    value = -int(digest[:8], 16)
    if value in {0, -11, -22}:
        value -= 101
    return value


def set_session(
    response: Response,
    secret: str,
    max_user_id: int,
    *,
    secure: bool = False,
    partitioned: bool = False,
) -> None:
    exp = int(time.time()) + TTL
    payload = f"{max_user_id}:{exp}"
    # Mini-app inside MAX needs SameSite=None + Partitioned. A normal browser
    # tab is first-party: Lax without Partitioned, otherwise Safari/Firefox
    # drop the cookie and the person looks "registered" in MAX but logged out
    # on the website.
    embedded = bool(secure and partitioned)
    response.set_cookie(
        COOKIE,
        f"{payload}:{_sign(secret, payload)}",
        httponly=True,
        secure=secure,
        samesite="none" if embedded else "lax",
        max_age=TTL,
        path="/",
    )
    if embedded:
        # CHIPS allows the HttpOnly session in MAX's embedded web client even
        # when unpartitioned third-party cookies are disabled. Python 3.11's
        # SimpleCookie does not yet expose the Partitioned attribute.
        response.raw_headers = [
            (
                name,
                value + b"; Partitioned"
                if name == b"set-cookie" and value.startswith(COOKIE.encode() + b"=")
                else value,
            )
            for name, value in response.raw_headers
        ]


def read_session(request: Request, secret: str) -> int | None:
    raw = request.cookies.get(COOKIE) or ""
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    user_id, exp, sig = parts
    payload = f"{user_id}:{exp}"
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return None
    try:
        if int(exp) < int(time.time()):
            return None
        return int(user_id)
    except ValueError:
        return None


def verify_init_data(raw: str, bot_token: str, *, max_age: int = 86400) -> dict[str, Any] | None:
    if not raw or not bot_token:
        return None
    pairs: list[list[str]] = []
    for chunk in raw.split("&"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        pairs.append([key, value])
    if len({key for key, _ in pairs}) != len(pairs):
        return None
    hash_items = [item for item in pairs if item[0] == "hash"]
    if len(hash_items) != 1:
        return None
    original_hash = unquote(hash_items[0][1])
    decoded = [[key, unquote(value)] for key, value in pairs]
    decoded.sort(key=lambda item: item[0])
    launch = "\n".join(f"{key}={value}" for key, value in decoded if key != "hash")
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(secret, launch.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, original_hash):
        return None
    fields = {key: value for key, value in decoded if key != "hash"}
    try:
        auth_date = int(fields.get("auth_date") or "0")
    except ValueError:
        return None
    if auth_date <= 0 or abs(int(time.time()) - auth_date) > max_age:
        return None
    user_raw = fields.get("user") or "{}"
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or user.get("id") is None:
        return None
    try:
        if isinstance(user["id"], bool) or int(user["id"]) <= 0:
            return None
        user["id"] = int(user["id"])
    except (TypeError, ValueError):
        return None
    return {"user": user, "start_param": fields.get("start_param") or ""}
