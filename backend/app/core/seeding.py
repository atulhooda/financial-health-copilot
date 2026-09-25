"""Every random process is seeded from here. sha256, never Python hash() (randomised per process)."""
from __future__ import annotations

import hashlib

import numpy as np


def seed_for(*parts: object) -> int:
    """Stable 63-bit seed from the global seed plus any identifying parts."""
    from app.core.config import get_settings

    key = "|".join([str(get_settings().seed), *map(str, parts)])
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") >> 1


def rng_for(*parts: object) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(seed_for(*parts)))
