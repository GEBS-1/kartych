from __future__ import annotations

from typing import Any

from app.config import Settings


def create_redis(settings: Settings) -> Any:
    from redis.asyncio import Redis

    return Redis.from_url(settings.redis_url, decode_responses=True)


async def ping_redis(client: Any) -> bool:
    return bool(await client.ping())


async def claim_idempotency(client: Any | None, key: str, ttl_seconds: int = 86_400) -> bool:
    """True, если ключ новый и событие можно обработать."""
    if client is None:
        return True
    created = await client.set(f"idem:{key}", "1", nx=True, ex=ttl_seconds)
    return bool(created)
