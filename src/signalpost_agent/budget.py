"""Shared budget governor for one batch run.

Fixes, relative to the reference starter kit (RESOURCE_BUDGET.md):
  * counts every REAL attempt (including retries and redirect hops), not
    one per logical "module" -- the kit was measured making 15 real
    attempts while reporting 5.
  * enforces a soft wall-clock deadline so a slow/hostile batch still
    finishes with a terminal envelope for every company (measured
    end-to-end in ADVERSARIAL_TEST_PLAN #22, #24).
  * a per-host circuit breaker stops hammering a host that is clearly down,
    instead of repeating a doomed pattern across many companies that
    happen to share a slow/broken host.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class BudgetGovernor:
    max_requests: int = 1500          # kept well under the 2,000 hard cap, see RESOURCE_BUDGET.md
    max_wall_seconds: float = 38 * 60  # soft stop before the 45-minute hard cap
    max_cost_usd: float = 9.0          # kept under the $10 hard cap
    circuit_breaker_threshold: int = 5  # consecutive failures on one host before it's cut off

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _requests_made: int = field(default=0, repr=False)
    _cost_spent: float = field(default=0.0, repr=False)
    _start_time: float = field(default_factory=time.monotonic, repr=False)
    _host_failures: dict = field(default_factory=dict, repr=False)
    _host_open: dict = field(default_factory=dict, repr=False)

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def deadline_passed(self) -> bool:
        return self.elapsed_seconds() >= self.max_wall_seconds

    def requests_remaining(self) -> int:
        with self._lock:
            return max(0, self.max_requests - self._requests_made)

    def allow_request(self) -> bool:
        """Read-only check, for callers that only need a quick look (e.g.
        deciding whether to even start a new redirect hop). Does NOT
        reserve a slot -- see reserve_request_slot() for that. Because
        this check and any later increment are two separate operations,
        two concurrent callers can both see "room available" before
        either one actually claims it, each then proceeds, and the total
        overshoots the cap. This was confirmed on a live run: the budget
        was configured for 1,500 but two concurrent runs each reached
        1,507. allow_request() is kept for non-reservation uses; anything
        that is about to actually make a request must use
        reserve_request_slot() instead.
        """
        if self.deadline_passed():
            return False
        with self._lock:
            return self._requests_made < self.max_requests

    def reserve_request_slot(self) -> bool:
        """Atomically checks AND claims one request slot in a single
        locked operation. Returns True iff the slot was granted. This is
        the race-free replacement for the old allow_request() +
        record_requests() two-step pattern: the check and the increment
        now happen together, under the same lock acquisition, so no
        other thread can observe the pre-increment count in between.
        """
        if self.deadline_passed():
            return False
        with self._lock:
            if self._requests_made < self.max_requests:
                self._requests_made += 1
                return True
            return False

    def record_requests(self, n: int) -> None:
        """Retained for callers that must record attempts that were made
        without going through reserve_request_slot() first (there should
        be none in production code after this fix; kept for tests and as
        a defensive fallback). Prefer reserve_request_slot() for anything
        that is about to make a real network attempt.
        """
        with self._lock:
            self._requests_made += n

    def record_cost(self, usd: float) -> bool:
        """Returns True if the cost was accepted, False if it would exceed
        the declared budget (caller must not spend it).
        """
        with self._lock:
            if self._cost_spent + usd > self.max_cost_usd:
                return False
            self._cost_spent += usd
            return True

    def host_of(self, url: str) -> str:
        return (urlparse(url).netloc or "").lower()

    def record_host_result(self, url: str, *, ok: bool) -> None:
        host = self.host_of(url)
        with self._lock:
            if ok:
                self._host_failures[host] = 0
            else:
                self._host_failures[host] = self._host_failures.get(host, 0) + 1
                if self._host_failures[host] >= self.circuit_breaker_threshold:
                    self._host_open[host] = True

    def host_circuit_open(self, url: str) -> bool:
        host = self.host_of(url)
        with self._lock:
            return self._host_open.get(host, False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "requests_made": self._requests_made,
                "requests_budget": self.max_requests,
                "elapsed_seconds": round(self.elapsed_seconds(), 1),
                "wall_budget_seconds": self.max_wall_seconds,
                "cost_spent_usd": round(self._cost_spent, 4),
                "cost_budget_usd": self.max_cost_usd,
                "hosts_circuit_open": [h for h, v in self._host_open.items() if v],
            }
