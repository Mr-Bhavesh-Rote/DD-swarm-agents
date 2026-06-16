"""Run progress event bus over Redis pub/sub (§6 SSE streaming).

The worker publishes progress events to `run:{run_id}:events`; the FastAPI SSE endpoint
subscribes and relays them to the browser. A terminal event (`status: done|failed|
cancelled`) tells the SSE loop to close.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict

import redis
import redis.asyncio as aredis

from app.core.config import get_settings


def _channel(run_id: str) -> str:
    return f"run:{run_id}:events"


def publish_event(run_id: str, event: Dict[str, Any]) -> None:
    """Synchronous publish (used by the sync worker)."""
    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        client.publish(_channel(run_id), json.dumps(event))
        # Keep a short replay buffer so late SSE subscribers still see recent progress.
        key = f"run:{run_id}:log"
        client.rpush(key, json.dumps(event))
        client.expire(key, 3600)
    finally:
        client.close()


async def replay_events(run_id: str) -> list[Dict[str, Any]]:
    settings = get_settings()
    client = aredis.from_url(settings.redis_url)
    try:
        items = await client.lrange(f"run:{run_id}:log", 0, -1)
        return [json.loads(i) for i in items]
    finally:
        await client.aclose()


async def subscribe_events(run_id: str) -> AsyncIterator[Dict[str, Any]]:
    """Async generator yielding events; replays the buffer first, then live events."""
    for ev in await replay_events(run_id):
        yield ev
        if ev.get("status") in ("done", "failed", "cancelled"):
            return

    settings = get_settings()
    client = aredis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    await pubsub.subscribe(_channel(run_id))
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            ev = json.loads(message["data"])
            yield ev
            if ev.get("status") in ("done", "failed", "cancelled"):
                return
    finally:
        await pubsub.unsubscribe(_channel(run_id))
        await pubsub.aclose()
        await client.aclose()


def heartbeat(run_id: str, ttl: int = 30) -> None:
    """Liveness beacon: refreshed by a background thread while a run executes. The key
    expires after `ttl` seconds, so a dead/killed worker stops refreshing it and the run is
    detectably stale even mid-call (when no progress events are emitted)."""
    import time

    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        client.set(f"run:{run_id}:hb", str(time.time()), ex=ttl)
    finally:
        client.close()


def heartbeat_age(run_id: str) -> float | None:
    """Seconds since the last heartbeat, or None if there is no (live) heartbeat."""
    import time

    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        v = client.get(f"run:{run_id}:hb")
        return (time.time() - float(v)) if v else None
    finally:
        client.close()


def clear_heartbeat(run_id: str) -> None:
    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        client.delete(f"run:{run_id}:hb")
    finally:
        client.close()


def is_cancelled(run_id: str) -> bool:
    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        return bool(client.get(f"run:{run_id}:cancel"))
    finally:
        client.close()


def request_cancel(run_id: str) -> None:
    settings = get_settings()
    client = redis.from_url(settings.redis_url)
    try:
        client.set(f"run:{run_id}:cancel", "1", ex=86400)
    finally:
        client.close()
