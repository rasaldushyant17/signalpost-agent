import json
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from verify_determinism import _strip_volatile_fields, _load  # noqa: E402


def _envelope(org, *, started_at, completed_at, retrieved_at, revenue):
    return {
        "organisation_number": org,
        "run": {"run_id": f"fixed-{org}", "started_at": started_at, "completed_at": completed_at},
        "claims": [{"claim_id": "c1", "field": "accounts.revenue", "value": revenue}],
        "evidence": [{"id": "ev1", "retrieved_at": retrieved_at, "source_url": "https://data.brreg.no/x"}],
    }


def test_same_content_different_timestamps_is_identical_after_stripping():
    a = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=1000)
    b = _envelope("923609016", started_at="t9", completed_at="t10", retrieved_at="t11", revenue=1000)
    assert _strip_volatile_fields(a) == _strip_volatile_fields(b)


def test_different_claim_value_is_a_real_difference():
    a = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=1000)
    b = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=2000)
    assert _strip_volatile_fields(a) != _strip_volatile_fields(b)


def test_different_run_id_is_a_real_difference_not_stripped():
    # run_id is deterministic in production (hash of org number only), so
    # two runs on the same company must produce the SAME run_id. If they
    # don't, that itself is a real bug this tool must catch, not hide.
    a = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=1000)
    b = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=1000)
    b["run"]["run_id"] = "different-run-id"
    assert _strip_volatile_fields(a) != _strip_volatile_fields(b)


def test_full_script_reports_ready_for_identical_runs(capsys):
    import verify_determinism

    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "run1.jsonl"
        p2 = Path(tmp) / "run2.jsonl"
        env1 = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=1000)
        env2 = _envelope("923609016", started_at="t9", completed_at="t10", retrieved_at="t11", revenue=1000)
        p1.write_text(json.dumps(env1) + "\n")
        p2.write_text(json.dumps(env2) + "\n")

        sys.argv = ["verify_determinism.py", str(p1), str(p2)]
        exit_code = verify_determinism.main()
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "READY" in out
        assert "NOT READY" not in out


def test_full_script_reports_not_ready_for_real_differences(capsys):
    import verify_determinism

    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "run1.jsonl"
        p2 = Path(tmp) / "run2.jsonl"
        env1 = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=1000)
        env2 = _envelope("923609016", started_at="t1", completed_at="t2", retrieved_at="t3", revenue=2000)
        p1.write_text(json.dumps(env1) + "\n")
        p2.write_text(json.dumps(env2) + "\n")

        sys.argv = ["verify_determinism.py", str(p1), str(p2)]
        exit_code = verify_determinism.main()
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "NOT READY" in out
