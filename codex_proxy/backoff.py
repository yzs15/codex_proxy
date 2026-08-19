"""Exponential backoff with full jitter.

Pure and dependency-free so it can be unit-tested deterministically by injecting
a fake ``jitter`` function.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable


def full_jitter(capped_delay: float) -> float:
    """AWS-style full jitter: sleep a random amount in ``[0, capped_delay]``."""
    return random.uniform(0.0, capped_delay)


@dataclass
class Backoff:
    initial: float
    multiplier: float
    maximum: float
    # Maps the deterministically-computed ceiling to the actual sleep duration.
    # Overridable in tests to remove randomness.
    jitter: Callable[[float], float] = full_jitter

    def ceiling(self, attempt: int) -> float:
        """Undithered delay ceiling for ``attempt`` (1 == first retry)."""
        if attempt < 1:
            attempt = 1
        raw = self.initial * (self.multiplier ** (attempt - 1))
        return min(raw, self.maximum)

    def delay(self, attempt: int) -> float:
        """Actual (jittered) delay to sleep before ``attempt``."""
        return self.jitter(self.ceiling(attempt))

    @classmethod
    def from_config(cls, cfg, jitter: Callable[[float], float] = full_jitter) -> "Backoff":
        return cls(
            initial=cfg.backoff_initial,
            multiplier=cfg.backoff_multiplier,
            maximum=cfg.backoff_max,
            jitter=jitter,
        )
