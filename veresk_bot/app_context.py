"""Общий контекст приложения (Redis и т.д.) для handlers."""

from __future__ import annotations

redis_client = None


def set_redis(redis) -> None:
    global redis_client
    redis_client = redis
