import threading
import time
from concurrent.futures import ThreadPoolExecutor

from signalpost_agent.budget import BudgetGovernor


def test_request_counting_and_cap():
    gov = BudgetGovernor(max_requests=5, max_wall_seconds=999)
    assert gov.allow_request()
    gov.record_requests(5)
    assert not gov.allow_request()
    assert gov.requests_remaining() == 0


def test_deadline_stops_new_requests():
    gov = BudgetGovernor(max_requests=9999, max_wall_seconds=0.05)
    assert gov.allow_request()
    time.sleep(0.1)
    assert not gov.allow_request()


def test_cost_budget_rejects_overspend():
    gov = BudgetGovernor(max_cost_usd=1.0)
    assert gov.record_cost(0.6) is True
    assert gov.record_cost(0.6) is False  # would exceed 1.0
    assert gov.record_cost(0.3) is True


def test_circuit_breaker_opens_after_threshold():
    gov = BudgetGovernor(circuit_breaker_threshold=3)
    url = "https://dead-host.example/x"
    assert not gov.host_circuit_open(url)
    for _ in range(3):
        gov.record_host_result(url, ok=False)
    assert gov.host_circuit_open(url)


def test_circuit_breaker_resets_on_success():
    gov = BudgetGovernor(circuit_breaker_threshold=3)
    url = "https://flaky-host.example/x"
    gov.record_host_result(url, ok=False)
    gov.record_host_result(url, ok=False)
    gov.record_host_result(url, ok=True)  # resets the counter
    gov.record_host_result(url, ok=False)
    gov.record_host_result(url, ok=False)
    assert not gov.host_circuit_open(url)


def test_reserve_request_slot_never_overshoots_under_real_concurrency():
    # Reproduces a real overshoot found on a live run: with the old
    # separate allow_request()-then-record_requests() pattern, many
    # threads racing near the cap could all pass the check before any of
    # them incremented, causing the total to exceed the configured cap
    # (configured 1,500; two separate live runs each reached 1,507). This
    # test hammers reserve_request_slot() from many real threads at once
    # and asserts the granted count is EXACTLY the cap, never more.
    cap = 500
    gov = BudgetGovernor(max_requests=cap, max_wall_seconds=999)
    granted = []
    lock = threading.Lock()

    def worker():
        if gov.reserve_request_slot():
            with lock:
                granted.append(1)

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(lambda _: worker(), range(cap * 3)))  # far more attempts than the cap

    assert len(granted) == cap
    assert gov.snapshot()["requests_made"] == cap


def test_snapshot_reports_state():
    gov = BudgetGovernor(max_requests=100)
    gov.record_requests(7)
    snap = gov.snapshot()
    assert snap["requests_made"] == 7
    assert snap["requests_budget"] == 100
