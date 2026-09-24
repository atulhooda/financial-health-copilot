import os
import uuid

import pytest

from app.events.bus import InProcessBus, RedisStreamsBus


def test_inprocess_bus_round_trip():
    bus = InProcessBus()
    bus.publish({"type": "data.ingested", "user_id": "u1", "as_of": "2026-09-20"})
    (eid, ev), = bus.read()
    assert ev["user_id"] == "u1" and bus.read() == []


def test_redis_streams_bus_acks_and_redelivers_unacked():
    import redis

    url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
    try:
        redis.Redis.from_url(url).ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Redis not reachable (run `make up`)")
    redis.Redis.from_url(url).flushdb()
    bus = RedisStreamsBus(url, consumer=f"t-{uuid.uuid4().hex[:6]}")
    bus.publish({"type": "data.ingested", "user_id": "u1", "as_of": "2026-09-20"})
    first = bus.read()
    assert [ev["user_id"] for _, ev in first] == ["u1"]
    again = bus.read()  # not acked yet: a crashed worker gets it again
    assert [eid for eid, _ in again] == [eid for eid, _ in first]
    bus.ack([eid for eid, _ in first])
    assert bus.read() == []
