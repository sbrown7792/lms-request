"""Token bucket per guest, keyed on their cookie.

Deliberately soft: a guest who clears their browser data gets a fresh bucket.
The point is to stop one enthusiast queueing forty songs, not to be airtight.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .store import Store


@dataclass
class Verdict:
    allowed: bool
    tokens_left: float
    retry_after: float = 0.0  # seconds until the next token, when refused

    @property
    def message(self) -> str:
        if self.allowed:
            return ""
        mins = max(1, round(self.retry_after / 60))
        return f"You've used your requests for now — try again in about {mins} min."


class RateLimiter:
    def __init__(self, store: Store, capacity: int, refill_seconds: float) -> None:
        self.store = store
        self.capacity = float(capacity)
        self.refill_seconds = float(refill_seconds)

    def _current(self, guest_id: str, now: float) -> float:
        saved = self.store.bucket(guest_id)
        if saved is None:
            return self.capacity
        tokens, updated = saved
        earned = (now - updated) / self.refill_seconds
        return min(self.capacity, tokens + earned)

    def peek(self, guest_id: str) -> float:
        return self._current(guest_id, time.time())

    def take(self, guest_id: str) -> Verdict:
        now = time.time()
        tokens = self._current(guest_id, now)
        if tokens < 1.0:
            self.store.save_bucket(guest_id, tokens, now)
            return Verdict(False, tokens, (1.0 - tokens) * self.refill_seconds)
        tokens -= 1.0
        self.store.save_bucket(guest_id, tokens, now)
        return Verdict(True, tokens)

    def refund(self, guest_id: str) -> None:
        """Give the token back when the request failed for our reasons."""
        now = time.time()
        tokens = min(self.capacity, self._current(guest_id, now) + 1.0)
        self.store.save_bucket(guest_id, tokens, now)
