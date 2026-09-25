"""EventBus (SPEC §7): Redis Streams by default, in-process for tests and as the no-Redis fallback."""
from __future__ import annotations

import json
from typing import Protocol

from app.core.config import get_settings

STREAM = "hisaab:events"
GROUP = "recompute"


class EventBus(Protocol):
    def publish(self, event: dict) -> str: ...
    def read(self, max_events: int = 1000, block_ms: int = 0) -> list[tuple[str, dict]]: ...
    def ack(self, ids: list[str]) -> None: ...


class InProcessBus:
    def __init__(self) -> None:
        self._queue: list[tuple[str, dict]] = []
        self._n = 0

    def publish(self, event: dict) -> str:
        self._n += 1
        eid = f"{self._n}-0"
        self._queue.append((eid, json.loads(json.dumps(event, default=str))))
        return eid

    def read(self, max_events: int = 1000, block_ms: int = 0) -> list[tuple[str, dict]]:
        out, self._queue = self._queue[:max_events], self._queue[max_events:]
        return out

    def ack(self, ids: list[str]) -> None:  # nothing to do: read() already removed them
        return None


class RedisStreamsBus:
    """XADD to one stream; a consumer group reads, and we XACK only after the snapshot is committed."""

    def __init__(self, url: str | None = None, consumer: str = "worker-1") -> None:
        import redis

        self.r = redis.Redis.from_url(url or get_settings().redis_url, decode_responses=True)
        self.consumer = consumer
        try:
            self.r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, event: dict) -> str:
        return self.r.xadd(STREAM, {"event": json.dumps(event, default=str)})

    def read(self, max_events: int = 1000, block_ms: int = 0) -> list[tuple[str, dict]]:
        # pending-but-unacked first (a worker that crashed mid-snapshot), then new messages
        out = []
        for start in ("0", ">"):
            resp = self.r.xreadgroup(GROUP, self.consumer, {STREAM: start}, count=max_events,
                                     block=block_ms if start == ">" and block_ms else None)
            for _stream, messages in resp or []:
                out += [(mid, json.loads(fields["event"])) for mid, fields in messages if fields]
        return out

    def ack(self, ids: list[str]) -> None:
        if ids:
            self.r.xack(STREAM, GROUP, *ids)


_inprocess: InProcessBus | None = None


def get_bus() -> EventBus:
    global _inprocess
    if get_settings().event_bus == "inprocess":
        _inprocess = _inprocess or InProcessBus()
        return _inprocess
    return RedisStreamsBus()
