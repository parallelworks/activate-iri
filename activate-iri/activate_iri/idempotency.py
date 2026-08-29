"""In-process idempotency store for the framework's Idempotency-Key support (single replica).

Multi-replica deployments use the Redis store shipped with the DOE demo adapter package
(demo_adapter.compute.idempotency.RedisIdempotencyStore) or an equivalent.
"""
from __future__ import annotations

import asyncio
import os
import time

from app.idempotency import IdempotencyStore, LockState


class InMemoryIdempotencyStore(IdempotencyStore):
    def __init__(self):
        self._items: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self.ttl = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "86400"))
        self.lock_ttl = int(os.environ.get("LOCK_TTL_SECONDS", "60"))

    def _expire(self) -> None:
        now = time.time()
        for key in [k for k, v in self._items.items() if v["expires"] < now]:
            self._items.pop(key, None)

    async def check_and_lock(self, cache_key: str, body_hash: str):
        async with self._lock:
            self._expire()
            item = self._items.get(cache_key)
            if item is None:
                self._items[cache_key] = {"state": LockState.LOCKED, "body_hash": body_hash, "expires": time.time() + self.lock_ttl}
                return ("proceed", None, None)
            if item["body_hash"] != body_hash:
                return ("fingerprint_mismatch", None, None)
            if item["state"] == LockState.LOCKED:
                return ("conflict", None, None)
            return ("hit", item["body"], item["status"])

    async def store_result(self, cache_key: str, body_hash: str, response_body: dict, response_status: int) -> None:
        async with self._lock:
            item = self._items.get(cache_key)
            if item and item["state"] == LockState.LOCKED and item["body_hash"] == body_hash:
                item.update({"state": LockState.DONE, "body": response_body, "status": response_status, "expires": time.time() + self.ttl})

    async def delete_lock(self, cache_key: str) -> None:
        async with self._lock:
            item = self._items.get(cache_key)
            if item and item["state"] == LockState.LOCKED:
                self._items.pop(cache_key, None)

    async def close(self) -> None:
        self._items.clear()
