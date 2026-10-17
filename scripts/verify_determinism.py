#!/usr/bin/env python3
"""Compares two envelope .jsonl files from two separate runs of the SAME
input companies, and reports whether they are semantically identical.

This directly implements the check an external evaluator asked for:
"Run the exact command twice and require zero semantic differences before
submitting." Two fields are legitimately expected to differ between any
two separate runs, and are excluded from the comparison for that reason:

  * evidence[].retrieved_at -- reflects the real moment a source was
    actually fetched, the same way it should for any live data agent.
  * run.started_at / run.completed_at -- bookkeeping about when the run
    happened, not a fact about any company.

run.run_id is NOT excluded: it is deterministic (derived from the
organisation number only, not randomness or time), so it must be
IDENTICAL between two runs on the same input -- if it differs, that is
itself a real bug, not something to paper over.

Usage:
    python3 scripts/verify_determinism.py out/envelopes_run1.jsonl out/envelopes_run2.jsonl
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path


def _strip_volatile_fields(envelope: dict) -> dict:
    env = copy.deepcopy(envelope)
    run = env.get("run", {})
    run.pop("started_at", None)
    run.pop("completed_at", None)
    for ev in env.get("evidence", []):
        ev.pop("retrieved_at", None)
    return env


def _load(path: str) -> dict[str, dict]:
    by_org = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            env = json.loads(line)
            by_org[env["organisation_number"]] = env
    return by_org


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run1", help="first run's envelopes .jsonl")
    parser.add_argument("run2", help="second run's envelopes .jsonl")
    args = parser.parse_args()

    run1 = _load(args.run1)
    run2 = _load(args.run2)

    all_orgs = sorted(set(run1) | set(run2))
    only_in_1 = sorted(set(run1) - set(run2))
    only_in_2 = sorted(set(run2) - set(run1))

    identical = 0
    differing: list[tuple[str, list[str]]] = []

    for org in all_orgs:
        if org not in run1 or org not in run2:
            continue
        a = _strip_volatile_fields(run1[org])
        b = _strip_volatile_fields(run2[org])
        if a == b:
            identical += 1
        else:
            # Report WHICH top-level keys differ, for a quick first look.
            diff_keys = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
            differing.append((org, diff_keys))

    print(f"Companies in run 1: {len(run1)}")
    print(f"Companies in run 2: {len(run2)}")
    print(f"Present in both:    {len(all_orgs) - len(only_in_1) - len(only_in_2)}")
    if only_in_1:
        print(f"Only in run 1 ({len(only_in_1)}): {only_in_1[:10]}{' ...' if len(only_in_1) > 10 else ''}")
    if only_in_2:
        print(f"Only in run 2 ({len(only_in_2)}): {only_in_2[:10]}{' ...' if len(only_in_2) > 10 else ''}")
    print()
    print(f"Identical (ignoring run timestamps and evidence retrieval times): {identical}")
    print(f"Differing: {len(differing)}")

    if differing:
        print()
        print("First 10 differing companies and which top-level sections changed:")
        for org, keys in differing[:10]:
            print(f"  {org}: {keys}")
        print()
        print("NOT READY: semantic differences remain. Do not submit this commit yet.")
        return 1

    if only_in_1 or only_in_2:
        print()
        print("NOT READY: the two runs did not cover the same set of companies.")
        return 1

    print()
    print("READY: the two runs are semantically identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
