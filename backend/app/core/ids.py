"""Deterministic ids: same inputs -> same id, on every run."""
from __future__ import annotations

import hashlib


def stable_hash(*parts: object, n: int = 12) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:n]


def stable_id(prefix: str, *parts: object, n: int = 12) -> str:
    return f"{prefix}_{stable_hash(*parts, n=n)}"
