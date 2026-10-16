"""Rate-Limit und Wiederholungen mit exponentiellem Backoff – nur für idempotente Abrufe.

Private, zustandsändernde Aufrufe (Orders) werden hiermit **nie** wiederholt; dort
gilt der Abgleich über die Client-Referenz (siehe ``live/executor.py``).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


@dataclass
class TokenBucket:
    """Einfacher Token-Bucket: ``rate`` Anfragen pro Sekunde, Burst bis ``capacity``."""

    rate: float = 1.0
    capacity: float = 2.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _tokens: float = field(default=-1.0, init=False)
    _last: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def acquire(self) -> float:
        """Blockiert, bis ein Token frei ist. Gibt die Wartezeit in Sekunden zurück."""
        with self._lock:
            now = self.clock()
            if self._tokens < 0:
                self._tokens, self._last = self.capacity, now
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            wait = 0.0
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                self.sleep(wait)
                self._last = self.clock()
                self._tokens = 1.0
            self._tokens -= 1.0
            return wait


class RetryExhaustedError(RuntimeError):
    pass


def retry_call(
    fn: Callable[[], T],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Ruft ``fn`` auf und wiederholt bei ``retry_on`` mit Backoff ``base * 2^n`` (+ Jitter)."""
    if attempts < 1:
        raise ValueError("attempts muss >= 1 sein")
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if attempt == attempts - 1:
                break
            delay = min(max_delay, base_delay * (2**attempt)) * (1 + 0.25 * jitter())
            if on_retry:
                on_retry(attempt + 1, exc, delay)
            sleep(delay)
    raise RetryExhaustedError(f"Nach {attempts} Versuchen fehlgeschlagen: {last}") from last
