import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from signalpost_agent.budget import BudgetGovernor
from signalpost_agent.http_client import BudgetedFetcher, ErrorKind


def _serve_once(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        try:
            conn.recv(4096)
            handler(conn)
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port, t


def test_truncated_body_does_not_raise():
    def handler(conn):
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 1000\r\n\r\n{\"a\":1"
        )
        # close without sending the rest -> IncompleteRead on the client
    port, t = _serve_once(handler)
    fetcher = BudgetedFetcher(max_attempts=1, enforce_public_ip=False,
                               connect_timeout=2, read_timeout=2)
    result = fetcher.get(f"http://127.0.0.1:{port}/x")
    t.join(timeout=2)
    # http.client silently returns a short body with no exception here --
    # verified empirically. The client must still catch this as truncation
    # by comparing against the declared Content-Length, not just rely on
    # an exception being raised.
    assert result.ok is False
    assert result.error_kind == ErrorKind.TRUNCATED
    assert result.attempts >= 1


def test_connection_reset_does_not_raise():
    def handler(conn):
        conn.close()  # reset immediately, no response at all
    port, t = _serve_once(handler)
    fetcher = BudgetedFetcher(max_attempts=1, enforce_public_ip=False,
                               connect_timeout=2, read_timeout=2)
    result = fetcher.get(f"http://127.0.0.1:{port}/x")
    t.join(timeout=2)
    assert result.ok is False
    assert result.status is None


def test_timeout_is_isolated_and_bounded():
    def handler(conn):
        time.sleep(3.0)  # longer than the client's timeout
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    port, t = _serve_once(handler)
    fetcher = BudgetedFetcher(max_attempts=1, enforce_public_ip=False,
                               connect_timeout=0.5, read_timeout=0.5)
    start = time.monotonic()
    result = fetcher.get(f"http://127.0.0.1:{port}/x")
    elapsed = time.monotonic() - start
    assert result.ok is False
    assert result.error_kind == ErrorKind.TIMEOUT
    assert elapsed < 2.5  # did not wait for the slow server


def test_404_is_not_retried():
    calls = {"n": 0}

    def handler(conn):
        calls["n"] += 1
        conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")

    port, t = _serve_once(handler)
    fetcher = BudgetedFetcher(max_attempts=3, enforce_public_ip=False,
                               connect_timeout=2, read_timeout=2)
    result = fetcher.get(f"http://127.0.0.1:{port}/x")
    t.join(timeout=2)
    assert result.status == 404
    assert result.attempts == 1  # no retry on a permanent client error


def test_200_ok_hashes_body():
    body = b'{"hello":"world"}'

    def handler(conn):
        payload = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        conn.sendall(payload)

    port, t = _serve_once(handler)
    fetcher = BudgetedFetcher(enforce_public_ip=False, connect_timeout=2, read_timeout=2)
    result = fetcher.get(f"http://127.0.0.1:{port}/x")
    t.join(timeout=2)
    assert result.ok is True
    assert result.body == body
    assert result.content_sha256 is not None


def test_private_ip_blocked_by_policy():
    fetcher = BudgetedFetcher(enforce_public_ip=True, max_attempts=1)
    result = fetcher.get("http://127.0.0.1:9/x")
    assert result.ok is False
    assert result.error_kind == "blocked_policy"


def test_per_host_concurrency_is_actually_limited():
    # Proves the fix for the real bug found on a live 1,000-company run:
    # many simultaneous connections to one host caused spurious failures.
    # Here a slow server records the highest number of connections it was
    # ever serving AT ONCE; with the limit set to 2, that peak must never
    # exceed 2 even when 8 requests are fired in parallel.
    current = {"n": 0, "peak": 0}
    lock = threading.Lock()

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]

    def serve_one():
        conn, _ = srv.accept()
        with lock:
            current["n"] += 1
            current["peak"] = max(current["peak"], current["n"])
        try:
            conn.recv(4096)
            time.sleep(0.15)  # hold the connection open so overlaps are measurable
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        finally:
            conn.close()
            with lock:
                current["n"] -= 1

    def accept_loop():
        for _ in range(8):
            serve_one()

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()

    fetcher = BudgetedFetcher(enforce_public_ip=False, max_concurrent_per_host=2,
                               requests_per_second_per_host=1000,  # isolate concurrency from pacing
                               connect_timeout=3, read_timeout=3)
    url = f"http://127.0.0.1:{port}/x"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: fetcher.get(url), range(8)))
    t.join(timeout=5)

    assert current["peak"] <= 2


def test_token_bucket_paces_sustained_requests_over_time():
    # Proves the actual fix for the real bug: a semaphore alone (previous
    # attempt) did not reduce the real failure rate on a live 1,000-company
    # run, because it only capped how many requests ran AT ONCE, not how
    # fast they went OVER TIME. Here, 5 fast, independent requests to one
    # host, paced at 2 requests/second, must take noticeably longer than
    # if they ran unpaced -- proving an actual rate, not just a concurrency
    # cap, is being enforced.
    def handler(conn):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    def serve_n(n):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(n)
        port = srv.getsockname()[1]

        def run():
            for _ in range(n):
                conn, _ = srv.accept()
                conn.recv(4096)
                handler(conn)
                conn.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return port, t

    port, t = serve_n(5)
    fetcher = BudgetedFetcher(enforce_public_ip=False, max_concurrent_per_host=10,
                               requests_per_second_per_host=2.0,  # 1 request every 0.5s, sustained
                               max_attempts=1, connect_timeout=2, read_timeout=2)
    url = f"http://127.0.0.1:{port}/x"
    start = time.monotonic()
    for _ in range(5):
        fetcher.get(url)
    elapsed = time.monotonic() - start
    t.join(timeout=2)
    # 5 requests at 2/sec (1 free "burst" token, then paced) must take at
    # least ~2 seconds; an unpaced client would finish in well under 1s.
    assert elapsed >= 1.8


def test_budget_is_checked_before_every_retry_not_just_once():
    # Reproduces a real overshoot found on a live 1,000-company run: a
    # governor allowing only 1 request must not let a single call's
    # internal retries (up to max_attempts) slip through uncounted.
    def handler(conn):
        conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")

    port, t = _serve_once(handler)
    gov = BudgetGovernor(max_requests=1, max_wall_seconds=999)
    fetcher = BudgetedFetcher(governor=gov, max_attempts=3, enforce_public_ip=False,
                               connect_timeout=2, read_timeout=2)
    fetcher.get(f"http://127.0.0.1:{port}/x")
    t.join(timeout=2)
    # Exactly 1 real attempt should have been made and counted, not 3.
    assert gov.snapshot()["requests_made"] == 1


def test_body_size_cap_enforced():
    big = b"x" * 200_000

    def handler(conn):
        payload = (
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(big)).encode() + b"\r\n\r\n" + big
        )
        conn.sendall(payload)

    port, t = _serve_once(handler)
    fetcher = BudgetedFetcher(enforce_public_ip=False, max_body_bytes=1000,
                               connect_timeout=2, read_timeout=2)
    result = fetcher.get(f"http://127.0.0.1:{port}/x")
    t.join(timeout=2)
    assert result.ok is False
    assert result.error_kind == ErrorKind.TOO_LARGE
