"""Builds the >=1,000 company submission manifest from the real universe
file, per COVERAGE_STRATEGY.md section 5: 600 uniform "population" +
~400 "stress" companies chosen to CONTAIN known hard cases (namesakes,
shared-host groups, non-AS forms, regulated industries, etc.) rather than
avoid them -- so identity/refresh bugs surface before the daily random
test finds them.

Selection is fully deterministic (stable hash of seed+org_number), and the
exact selection is recorded with its own SHA-256 so the manifest is
reproducible from the same universe file and seed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse

DEFAULT_SEED = "signalpost-agent-v1"


def iter_universe(path: str) -> Iterator[dict]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def stable_rank(seed: str, org_number: str) -> float:
    """Deterministic pseudo-random rank in [0, 1), stable across runs and
    independent of file order.
    """
    digest = hashlib.sha256(f"{seed}:{org_number}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)


def _host(website: str) -> str:
    if not website:
        return ""
    url = website if website.startswith("http") else f"http://{website}"
    try:
        return (urlparse(url).netloc or urlparse(url).path or "").lower().lstrip("www.")
    except Exception:
        return website.lower()


NAME_JUNK_CHARS = set("æøåÆØÅ&-0123456789")


@dataclass
class StratumSpec:
    name: str
    size: int
    predicate: callable  # (row, context) -> bool


def normalise_name(name: str) -> str:
    return " ".join((name or "").upper().split())


def build_context(rows: list[dict]) -> dict:
    """Precomputes cross-row facts needed by some strata (namesakes,
    shared website hosts) in one pass over the universe.
    """
    name_counts = Counter(normalise_name(r["name"]) for r in rows)
    host_counts = Counter()
    for r in rows:
        h = _host(r.get("website", ""))
        if h:
            host_counts[h] += 1
    return {"name_counts": name_counts, "host_counts": host_counts}


def _has_website(row: dict) -> bool:
    return bool((row.get("website") or "").strip())


STRATA: list[StratumSpec] = [
    StratumSpec("S1_rich_site_20plus_employees", 50,
                lambda r, ctx: _has_website(r) and (r.get("employees") or 0) >= 20),
    StratumSpec("S2_site_but_shell_like_industry", 30,
                lambda r, ctx: _has_website(r) and (r.get("industry_code") or "").startswith(("64", "68", "70"))),
    StratumSpec("S3_no_site_real_employees", 40,
                lambda r, ctx: not _has_website(r) and (r.get("employees") or 0) >= 5),
    StratumSpec("S4_no_site_no_employees_shell_industry", 30,
                lambda r, ctx: not _has_website(r) and not r.get("employees")
                and (r.get("industry_code") or "").startswith(("68", "00"))),
    StratumSpec("S5_large_or_asa", 30,
                lambda r, ctx: r.get("legal_form") == "ASA" or (r.get("employees") or 0) >= 100),
    StratumSpec("S6_non_as_legal_forms", 50,
                lambda r, ctx: r.get("legal_form") not in ("AS", "ASA")),
    StratumSpec("S7_namesake_names", 30,
                lambda r, ctx: ctx["name_counts"].get(normalise_name(r["name"]), 0) >= 2),
    StratumSpec("S8_unusual_characters_in_name", 30,
                lambda r, ctx: any(ch in NAME_JUNK_CHARS for ch in (r.get("name") or ""))
                and len(r.get("name") or "") > 0),
    StratumSpec("S9_shared_website_host", 40,
                lambda r, ctx: _has_website(r) and ctx["host_counts"].get(_host(r.get("website", "")), 0) >= 2),
    StratumSpec("S10_bankrupt_or_liquidating", 20,
                lambda r, ctx: bool(r.get("bankrupt")) or bool(r.get("liquidating"))),
    StratumSpec("S11_regulated_financial_industry", 20,
                lambda r, ctx: (r.get("industry_code") or "").startswith(("64", "65", "66"))),
    StratumSpec("S12_non_no_or_malformed_website", 30,
                lambda r, ctx: _has_website(r) and not r.get("website", "").rstrip("/").lower().endswith(
                    (".no", ".no/"))),
]

POPULATION_SIZE = 600


def build_manifest(universe_path: str, *, seed: str = DEFAULT_SEED) -> dict:
    """Returns {"manifest": [org_numbers...], "strata": {name: [org...]},
    "selection_sha256": str, "universe_row_count": int}.
    """
    rows = list(iter_universe(universe_path))
    ctx = build_context(rows)

    by_org = {r["organisation_number"]: r for r in rows}
    used: set[str] = set()
    strata_selected: dict[str, list[str]] = {}

    shortfall = 0
    for spec in STRATA:
        candidates = [r for r in rows if r["organisation_number"] not in used and spec.predicate(r, ctx)]
        candidates.sort(key=lambda r: stable_rank(seed + ":" + spec.name, r["organisation_number"]))
        chosen = [r["organisation_number"] for r in candidates[: spec.size]]
        strata_selected[spec.name] = chosen
        used.update(chosen)
        shortfall += spec.size - len(chosen)

    # Any stratum that couldn't fill (e.g. very few regulated-industry
    # companies in a particular universe snapshot) has its shortfall made
    # up from the uniform population draw, so the manifest still reaches
    # its target size rather than silently coming up short.
    population_target = POPULATION_SIZE + max(0, shortfall)
    population_candidates = [r for r in rows if r["organisation_number"] not in used]
    population_candidates.sort(key=lambda r: stable_rank(seed + ":population", r["organisation_number"]))
    population = [r["organisation_number"] for r in population_candidates[:population_target]]
    used.update(population)

    manifest = population + [org for orgs in strata_selected.values() for org in orgs]
    manifest = sorted(set(manifest))  # sorted, de-duplicated, deterministic order in the output file

    blob = "\n".join(manifest).encode("utf-8")
    selection_sha256 = hashlib.sha256(blob).hexdigest()

    return {
        "manifest": manifest,
        "population": population,
        "strata": strata_selected,
        "selection_sha256": selection_sha256,
        "universe_row_count": len(rows),
        "seed": seed,
        "by_org": {org: by_org[org] for org in manifest if org in by_org},
    }


def write_manifest_files(result: dict, out_dir: str) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    manifest_txt = out / "organisation_numbers.txt"
    manifest_txt.write_text("\n".join(result["manifest"]) + "\n", encoding="utf-8")

    manifest_jsonl = out / "organisation_numbers.jsonl"
    with manifest_jsonl.open("w", encoding="utf-8") as fh:
        for org in result["manifest"]:
            row = result["by_org"].get(org, {"organisation_number": org})
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    strata_report = out / "manifest_composition.json"
    strata_report.write_text(json.dumps({
        "seed": result["seed"],
        "universe_row_count": result["universe_row_count"],
        "manifest_size": len(result["manifest"]),
        "population_size": len(result["population"]),
        "strata_sizes": {k: len(v) for k, v in result["strata"].items()},
        "selection_sha256": result["selection_sha256"],
    }, indent=2), encoding="utf-8")

    return {
        "organisation_numbers_txt": str(manifest_txt),
        "organisation_numbers_jsonl": str(manifest_jsonl),
        "manifest_composition_json": str(strata_report),
    }
