from __future__ import annotations

import redis

from app.config import get_settings

_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Prea multe incercari. Reincearca peste {retry_after_seconds}s.")


def check_fixed_window(key: str, limit: int, window_seconds: int) -> int:
    """Limitare cu fereastra fixa in Redis. Returneaza numarul curent de hit-uri.
    Ridica RateLimitExceeded daca limita a fost depasita."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.incr(key, 1)
    pipe.ttl(key)
    count, ttl = pipe.execute()
    if ttl is None or ttl < 0:
        r.expire(key, window_seconds)
        ttl = window_seconds
    if count > limit:
        raise RateLimitExceeded(retry_after_seconds=max(ttl, 1))
    return count


def reset_key(key: str) -> None:
    get_redis().delete(key)
