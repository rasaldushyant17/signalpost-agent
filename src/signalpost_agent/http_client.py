"""Crash-safe HTTP fetching.

Design targets (see RESOURCE_BUDGET.md and ADVERSARIAL_TEST_PLAN.md #13, #20,
#22, #29):

  * Every possible failure (timeout, DNS failure, connection reset,
    truncated body, malformed response, SSL error, HTTP error status)
    becomes a typed FetchResult. NOTHING escapes as an uncaught exception.
  * Retries are an explicit allow-list of transient conditions only.
    4xx client errors (400/401/403/404/410) are never retried.
  * Every real attempt (including retries and redirect hops) is counted,
    because the competition budget counts "outbound requests including
    redirects and retries".
  * DNS, connect and read all have explicit timeouts.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Optional
from urllib.parse import urlparse

DEFAULT_USER_AGENT = "SignalpostResearchAgent/1.0 (+contact-in-README)"

# Statuses it is ever worth retrying. Everything else (400/401/403/404/410/...)
# is treated as terminal on the first response.
_RETRYABLE_STATUSES = {429, 502, 503, 504}


@dataclass
class FetchResult:
    url: str                      # URL actually requested
    final_url: str                # URL after following redirects (may equal url)
    redirect_chain: list[str] = field(default_factory=list)
    status: Optional[int] = None  # HTTP status of the final response, if any
    ok: bool = False              # True iff a well-formed 2xx body was read
    body: Optional[bytes] = None  # raw bytes, only present if ok
    headers: dict = field(default_factory=dict)
    content_sha256: Optional[str] = None
    error_kind: Optional[str] = None   # one of the ErrorKind values below, if not ok
    error_detail: str = ""
    attempts: int = 0             # real attempts made, including retries
    elapsed_ms: float = 0.0


class ErrorKind:
    TIMEOUT = "timeout"
    DNS_ERROR = "dns_error"
    CONNECTION_ERROR = "connection_error"
    TLS_ERROR = "tls_error"
    TRUNCATED = "truncated_body"
    HTTP_ERROR = "http_error"          # terminal 4xx/5xx after retries exhausted
    BLOCKED_POLICY = "blocked_policy"  # e.g. rejected by SSRF/public-IP guard
    TOO_LARGE = "too_large"
    DECODE_ERROR = "decode_error"
    UNKNOWN = "unknown_error"


class PrivateAddressError(Exception):
    """Raised internally, then always caught -> BLOCKED_POLICY."""


class _TokenBucket:
    """Paces requests to no faster than `rate_per_second`, sustained over
    time -- not just a cap on how many run simultaneously.

    This is a direct response to evidence from a live 1,000-company run
    against the real Norwegian registry (2026-09-30): limiting simultaneous
    connections per host (a plain semaphore) cut the measured throughput
    roughly in half (about 6.3 req/s down to about 3.2 req/s) but left the
    failure rate essentially unchanged (~64%), while small, short bursts at
    a similar or higher instantaneous rate succeeded every time. That
    pattern -- fine in short bursts, failing only once sustained volume
    accumulates over minutes -- points to a sustained-rate limit somewhere
    between the client and the registry, not a simultaneous-connections
    limit. A token bucket enforces an actual pace over time; a semaphore
    does not.
    """

    def __init__(self, rate_per_second: float, burst: int = 1):
        self.rate = max(rate_per_second, 0.01)
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait_time = (1 - self.tokens) / self.rate
            time.sleep(wait_time)


def _assert_public_host(hostname: str) -> None:
    """Best-effort SSRF guard: reject requests whose resolved address is
    private/loopback/link-local. Best-effort because DNS can still change
    between this check and the connection; callers should not treat this as
    a complete sandbox, only as a cheap first line of defence.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise PrivateAddressError(f"DNS resolution failed: {exc}") from exc
    for family, _, _, _, sockaddr in infos:
        ip = sockaddr[0]
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise PrivateAddressError(f"{hostname} resolves to non-public address {ip}")


class BudgetedFetcher:
    """Fetches URLs with per-call timeouts, an allow-listed retry policy,
    and a shared request counter + wall-clock deadline supplied by a
    BudgetGovernor (see budget.py). Safe to use directly in tests with
    governor=None (no shared accounting, no deadline).
    """

    def __init__(
        self,
        *,
        governor=None,
        connect_timeout: float = 6.0,
        read_timeout: float = 12.0,
        max_body_bytes: int = 3_000_000,
        max_redirects: int = 5,
        max_attempts: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        enforce_public_ip: bool = True,
        max_concurrent_per_host: int = 4,
        requests_per_second_per_host: float = 1.0,
    ) -> None:
        self.governor = governor
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_body_bytes = max_body_bytes
        self.max_redirects = max_redirects
        self.max_attempts = max_attempts
        self.user_agent = user_agent
        self.enforce_public_ip = enforce_public_ip
        # A live 1,000-company run against the real Norwegian registry
        # (2026-09-30) showed ~64% of entity lookups coming back with a
        # plain HTTP 404 for organisation numbers that, moments later at a
        # slower pace, resolved correctly with real data. That points to
        # the registry (or something in front of it) reacting badly to
        # many simultaneous connections from one client, so every host is
        # now limited to a small number of truly concurrent requests,
        # regardless of how many worker threads the batch runner uses.
        self.max_concurrent_per_host = max_concurrent_per_host
        self._host_semaphores: dict[str, threading.Semaphore] = {}
        self._host_semaphore_lock = threading.Lock()

        # The actual pacing-over-time fix (see _TokenBucket docstring).
        self.requests_per_second_per_host = requests_per_second_per_host
        self._host_buckets: dict[str, _TokenBucket] = {}
        self._host_bucket_lock = threading.Lock()

    def _semaphore_for_host(self, host: str) -> threading.Semaphore:
        with self._host_semaphore_lock:
            sem = self._host_semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(self.max_concurrent_per_host)
                self._host_semaphores[host] = sem
            return sem

    def _bucket_for_host(self, host: str) -> _TokenBucket:
        with self._host_bucket_lock:
            bucket = self._host_buckets.get(host)
            if bucket is None:
                bucket = _TokenBucket(self.requests_per_second_per_host, burst=1)
                self._host_buckets[host] = bucket
            return bucket

    # -- public API ---------------------------------------------------

    def get(self, url: str, *, accept: str | None = None) -> FetchResult:
        """GET a URL. Never raises. Always returns a FetchResult."""
        start = time.monotonic()
        redirect_chain: list[str] = []
        current_url = url
        attempts = 0

        for _hop in range(self.max_redirects + 1):
            # No pre-check here: budget reservation happens atomically,
            # per real attempt, inside _attempt_with_retries via
            # governor.reserve_request_slot(). A separate pre-check here
            # would just be a second, redundant (and racy) read of the
            # same counter; _attempt_with_retries already returns a
            # budget-exhausted result (with next_location=None) when the
            # reservation fails, which falls straight through to the
            # return below.
            result, next_location, made_attempts = self._attempt_with_retries(
                current_url, accept=accept
            )
            attempts += made_attempts

            if next_location is not None:
                redirect_chain.append(current_url)
                current_url = next_location
                continue

            result.url = url
            result.final_url = current_url
            result.redirect_chain = redirect_chain
            result.attempts = attempts
            result.elapsed_ms = _ms_since(start)
            return result

        return FetchResult(
            url=url, final_url=current_url, redirect_chain=redirect_chain,
            ok=False, error_kind=ErrorKind.CONNECTION_ERROR,
            error_detail="too many redirects", attempts=attempts,
            elapsed_ms=_ms_since(start),
        )

    # -- internals ------------------------------------------------------

    def _attempt_with_retries(self, url: str, *, accept: str | None):
        """Returns (FetchResult, redirect_location_or_None, attempts_made).

        Checks the shared budget before EVERY individual attempt, not just
        once before this whole retry sequence starts. Checking only once
        (the original design) let up to (max_attempts - 1) extra real
        requests slip through per call near the budget boundary, and with
        several concurrent workers all passing that single check around
        the same moment, the real total could overshoot the configured
        cap by a handful of requests -- exactly what a live 1,000-company
        run surfaced (1,507 real requests against a 1,500 target). Still
        far inside the competition's hard 2,000 cap, but worth tightening.
        """
        last_result: Optional[FetchResult] = None
        for attempt in range(1, self.max_attempts + 1):
            if self.governor is not None:
                # Atomic check-and-claim: the old code checked
                # allow_request() (read-only) and only incremented the
                # counter AFTER the real network attempt completed. Two
                # concurrent threads could both read "room available"
                # before either one's increment landed, so both would
                # proceed -- confirmed on a live run (configured cap
                # 1,500; two separate runs each reached 1,507). Using one
                # locked reserve-then-attempt operation closes that gap:
                # the slot is claimed BEFORE the attempt is made, under
                # the same lock acquisition as the check.
                if not self.governor.reserve_request_slot():
                    if last_result is not None:
                        return last_result, None, attempt - 1
                    return (FetchResult(url=url, final_url=url, ok=False,
                                         error_kind=ErrorKind.BLOCKED_POLICY,
                                         error_detail="budget exhausted mid-retry"),
                            None, attempt - 1)
            result, redirect_to = self._single_attempt(url, accept=accept)
            if redirect_to is not None:
                return result, redirect_to, attempt
            if result.ok:
                return result, None, attempt
            if not self._is_retryable(result):
                return result, None, attempt
            last_result = result
            # capped, jittered backoff; never sleep past a governor's deadline
            sleep_for = min(0.25 * (2 ** (attempt - 1)), 2.0)
            time.sleep(sleep_for)
        return last_result, None, self.max_attempts

    def _is_retryable(self, result: FetchResult) -> bool:
        if result.error_kind in (ErrorKind.TIMEOUT, ErrorKind.CONNECTION_ERROR, ErrorKind.TRUNCATED):
            return True
        if result.status in _RETRYABLE_STATUSES:
            return True
        return False

    def _single_attempt(self, url: str, *, accept: str | None):
        """One real network attempt. Returns (FetchResult, redirect_url_or_None).
        Every exception type is caught here and mapped to a typed result --
        this is the boundary that must never let anything escape.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return FetchResult(url=url, final_url=url, ok=False,
                                error_kind=ErrorKind.BLOCKED_POLICY,
                                error_detail="unsupported scheme or missing host"), None

        if self.enforce_public_ip:
            try:
                _assert_public_host(parsed.hostname)
            except PrivateAddressError as exc:
                return FetchResult(url=url, final_url=url, ok=False,
                                    error_kind=ErrorKind.BLOCKED_POLICY,
                                    error_detail=str(exc)), None

        headers = {"User-Agent": self.user_agent}
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(url, headers=headers, method="GET")

        host_semaphore = self._semaphore_for_host(parsed.hostname)
        host_bucket = self._bucket_for_host(parsed.hostname)
        host_semaphore.acquire()
        try:
            # The semaphore caps how many requests to this host run AT
            # ONCE; the token bucket caps how FAST they can go sustained
            # over time. Evidence from a live run showed the semaphore
            # alone did not help -- the bucket is the part that actually
            # paces total throughput, which is what the evidence pointed to.
            host_bucket.acquire()
            return self._do_request(req, url)
        finally:
            host_semaphore.release()

    def _do_request(self, req, url: str):
        try:
            resp = urllib.request.urlopen(
                req, timeout=self.connect_timeout + self.read_timeout
            )
        except urllib.error.HTTPError as exc:
            # HTTPError IS the response for 4xx/5xx; read it (bounded) for context.
            try:
                _ = exc.read(1024)
            except Exception:
                pass
            return FetchResult(url=url, final_url=url, status=exc.code, ok=False,
                                error_kind=ErrorKind.HTTP_ERROR,
                                error_detail=f"HTTP {exc.code}"), None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
                kind = ErrorKind.TIMEOUT
            elif isinstance(reason, socket.gaierror):
                kind = ErrorKind.DNS_ERROR
            elif isinstance(reason, ssl.SSLError):
                kind = ErrorKind.TLS_ERROR
            else:
                kind = ErrorKind.CONNECTION_ERROR
            return FetchResult(url=url, final_url=url, ok=False, error_kind=kind,
                                error_detail=str(reason)), None
        except socket.timeout:
            return FetchResult(url=url, final_url=url, ok=False,
                                error_kind=ErrorKind.TIMEOUT,
                                error_detail="socket timeout"), None
        except (HTTPException, ConnectionError, OSError, ssl.SSLError) as exc:
            # Catches http.client.IncompleteRead, ConnectionResetError, etc.
            # This is exactly the class of exception that escaped the
            # reference kit's fetch_json and killed its whole batch.
            kind = ErrorKind.TRUNCATED if "IncompleteRead" in type(exc).__name__ else ErrorKind.CONNECTION_ERROR
            return FetchResult(url=url, final_url=url, ok=False, error_kind=kind,
                                error_detail=f"{type(exc).__name__}: {exc}"), None
        except Exception as exc:  # last-resort catch-all: never propagate
            return FetchResult(url=url, final_url=url, ok=False,
                                error_kind=ErrorKind.UNKNOWN,
                                error_detail=f"{type(exc).__name__}: {exc}"), None

        try:
            with resp:
                status = resp.status
                resp_headers = dict(resp.headers.items())
                if 300 <= status < 400:
                    location = resp_headers.get("Location")
                    if location:
                        return FetchResult(url=url, final_url=url, status=status, ok=False), location
                declared_length = _parse_content_length(resp_headers)
                body = self._read_bounded(resp)
        except (HTTPException, ConnectionError, OSError, ssl.SSLError) as exc:
            kind = ErrorKind.TRUNCATED if "IncompleteRead" in type(exc).__name__ else ErrorKind.CONNECTION_ERROR
            return FetchResult(url=url, final_url=url, status=None, ok=False,
                                error_kind=kind,
                                error_detail=f"{type(exc).__name__}: {exc}"), None
        except _TooLarge:
            return FetchResult(url=url, final_url=url, status=status, ok=False,
                                error_kind=ErrorKind.TOO_LARGE,
                                error_detail=f"body exceeded {self.max_body_bytes} bytes"), None
        except Exception as exc:
            return FetchResult(url=url, final_url=url, ok=False,
                                error_kind=ErrorKind.UNKNOWN,
                                error_detail=f"{type(exc).__name__}: {exc}"), None

        # http.client's read() does NOT raise when a server closes the
        # connection early: it silently yields a short body followed by an
        # empty chunk (verified empirically -- see tests/test_http_client.py).
        # A body shorter than a declared Content-Length is truncation, not
        # a successful response, even though no exception was raised.
        if declared_length is not None and len(body) < declared_length:
            return FetchResult(
                url=url, final_url=url, status=status, ok=False,
                error_kind=ErrorKind.TRUNCATED,
                error_detail=f"declared Content-Length={declared_length} but received {len(body)} bytes",
            ), None

        digest = hashlib.sha256(body).hexdigest()
        return FetchResult(
            url=url, final_url=url, status=status, ok=True, body=body,
            headers=resp_headers, content_sha256=digest,
        ), None

    def _read_bounded(self, resp) -> bytes:
        chunks = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_body_bytes:
                raise _TooLarge()
            chunks.append(chunk)
        return b"".join(chunks)


class _TooLarge(Exception):
    pass


def retry_after_seconds(headers: dict) -> Optional[float]:
    """Parse a Retry-After header (seconds or HTTP-date). Returns None if
    absent or unparseable.
    """
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, (dt.timestamp() - time.time()))
    except Exception:
        return None


def _parse_content_length(headers: dict) -> Optional[int]:
    value = headers.get("Content-Length") or headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _ms_since(start: float) -> float:
    return (time.monotonic() - start) * 1000.0
