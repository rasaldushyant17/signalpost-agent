import gzip
import json
import tempfile
from pathlib import Path

from signalpost_agent.sampling import build_manifest, stable_rank, iter_universe


def _make_universe(path, n=2000):
    rows = []
    for i in range(n):
        org = str(900000000 + i)
        rows.append({
            "organisation_number": org,
            "name": f"COMPANY {i} AS" if i % 50 != 0 else "DUPLICATE NAME AS",
            "legal_form": "AS" if i % 10 != 0 else "ENK",
            "employees": (i % 30) if i % 3 == 0 else None,
            "bankrupt": i % 200 == 0,
            "liquidating": False,
            "municipality": "OSLO",
            "municipality_number": "0301",
            "industry_code": "68.200" if i % 4 == 0 else ("64.191" if i % 37 == 0 else "62.010"),
            "industry_label": "x",
            "website": (f"company{i}.com" if i % 41 == 0 else f"company{i}.no") if i % 5 == 0 else "",
            "latest_submitted_accounts": "2025",
        })
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return rows


def test_stable_rank_deterministic():
    a = stable_rank("seed1", "923609016")
    b = stable_rank("seed1", "923609016")
    assert a == b


def test_stable_rank_differs_by_org():
    a = stable_rank("seed1", "923609016")
    b = stable_rank("seed1", "915637353")
    assert a != b


def test_build_manifest_is_deterministic_across_two_runs():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.jsonl.gz"
        _make_universe(path)
        result1 = build_manifest(str(path), seed="test-seed")
        result2 = build_manifest(str(path), seed="test-seed")
        assert result1["manifest"] == result2["manifest"]
        assert result1["selection_sha256"] == result2["selection_sha256"]


def test_build_manifest_has_no_duplicates_and_valid_orgs():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.jsonl.gz"
        _make_universe(path)
        result = build_manifest(str(path), seed="test-seed")
        assert len(result["manifest"]) == len(set(result["manifest"]))
        for org in result["manifest"]:
            assert len(org) == 9 and org.isdigit()


def test_build_manifest_reaches_minimum_size():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.jsonl.gz"
        _make_universe(path, n=3000)  # large enough to fill every stratum
        result = build_manifest(str(path), seed="test-seed")
        assert len(result["manifest"]) >= 1000


def test_build_manifest_includes_non_as_legal_forms():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.jsonl.gz"
        _make_universe(path, n=3000)
        result = build_manifest(str(path), seed="test-seed")
        forms = {result["by_org"][org]["legal_form"] for org in result["manifest"]}
        assert "ENK" in forms


def test_build_manifest_includes_namesake_stratum():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "universe.jsonl.gz"
        _make_universe(path, n=3000)
        result = build_manifest(str(path), seed="test-seed")
        assert len(result["strata"]["S7_namesake_names"]) > 0
