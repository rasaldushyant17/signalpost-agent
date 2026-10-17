#!/usr/bin/env python3
"""Builds the submission manifest (>=1,000 organisation numbers) from the
official universe file.

Usage:
    python3 scripts/build_manifest.py \
        --universe /path/to/signalpost-company-universe-2025.jsonl.gz \
        --out manifest/ \
        --seed signalpost-agent-v1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from signalpost_agent.sampling import build_manifest, write_manifest_files, DEFAULT_SEED


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", required=True, help="path to the .jsonl or .jsonl.gz universe file")
    parser.add_argument("--out", default="manifest", help="output directory")
    parser.add_argument("--seed", default=DEFAULT_SEED, help="deterministic selection seed")
    args = parser.parse_args()

    result = build_manifest(args.universe, seed=args.seed)
    paths = write_manifest_files(result, args.out)

    print(f"Universe rows scanned: {result['universe_row_count']}")
    print(f"Manifest size:         {len(result['manifest'])}")
    print(f"Population draw:       {len(result['population'])}")
    print(f"Selection SHA-256:     {result['selection_sha256']}")
    print("Stratum sizes:")
    for name, orgs in result["strata"].items():
        print(f"  {name}: {len(orgs)}")
    print("\nWrote:")
    for label, path in paths.items():
        print(f"  {label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
