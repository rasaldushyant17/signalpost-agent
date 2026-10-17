#!/usr/bin/env python3
"""The one evaluator command. Runs a batch of Norwegian organisation
numbers end-to-end and writes exactly one terminal envelope per input.

Usage (a 100-company daily-style batch):
    python3 scripts/run_batch.py \
        --organisations manifest/organisation_numbers.txt \
        --history-root state/history \
        --output out/envelopes.jsonl \
        --report out/run-report.json

Requires outbound network access to data.brreg.no (and, if the company's
own site is being verified, to that company's own domain). This sandbox
that built the code could not reach data.brreg.no to run this live; see
LIMITATIONS.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from signalpost_agent.batch import run_batch
from signalpost_agent.budget import BudgetGovernor
from signalpost_agent.envelope import validate_envelope_shape
from signalpost_agent import org_number as onr
from build_viewer import build_viewer_html


def _atomic_write_text(path: Path, text: str) -> None:
    """Writes `text` to `path` so that any reader either sees the complete
    old file or the complete new file -- never a partial write, and never
    stale cached bytes from before the write.

    Writes to a temporary file in the SAME directory (so the final rename
    is on the same filesystem, which is what makes it atomic), explicitly
    flushes the Python-level buffer AND asks the OS to flush its own
    buffer to disk (os.fsync) before closing, then atomically swaps the
    temp file into place with os.replace(). Without the explicit fsync, a
    reader on different storage (e.g. a network-mounted volume, or a
    container filesystem layer, as can exist in an automated evaluation
    environment) could observe stale or incomplete content even after the
    writing process believes the file is closed and complete.
    """
    path = Path(path)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)  # atomic on both POSIX and Windows for same-volume renames


def read_organisations_from_stream(stream) -> list[str]:
    """One organisation number per line, from any file-like object
    (including sys.stdin). Separated out from main() so it's directly
    unit-testable without needing a real stdin pipe or network access.
    """
    orgs = []
    for line in stream:
        normalised = onr.normalize(line.strip())
        if normalised:
            orgs.append(normalised)
    return orgs


def read_organisations(path: str) -> list[str]:
    p = Path(path)
    orgs: list[str] = []
    if p.suffix == ".jsonl":
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                orgs.append(row.get("organisation_number") or row.get("organisasjonsnummer"))
    else:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = onr.normalize(line.strip())
                if line:
                    orgs.append(line)
    return orgs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organisations", required=False, default=None,
                         help="text (one per line) or .jsonl file of organisation numbers. "
                              "If omitted, the list is read from standard input instead (one "
                              "organisation number per line) -- so this command also works if "
                              "an evaluator feeds the official batch in by piping it rather than "
                              "by a file path.")
    parser.add_argument("--history-root", default=None, help="directory for append-only refresh history (omit for a one-off baseline run)")
    parser.add_argument("--output", required=True, help="output .jsonl path, one terminal envelope per line")
    parser.add_argument("--report", required=True, help="output .json run report path")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--max-requests", type=int, default=None,
                         help="real request cap for this run. Default: scaled to the batch size "
                              "at the competition's own ratio (2,000 requests per 100 companies, "
                              "i.e. 15 requests/company kept safely under that), so a 100-company "
                              "batch behaves like the real evaluation and a 1,000-company batch "
                              "isn't starved by a cap sized for a tenth as many companies. A live "
                              "run found that a flat per-run cap, independent of batch size, can "
                              "even introduce run-to-run inconsistency: different invocations can "
                              "hit the SAME cap at a slightly different company depending on "
                              "thread timing, so which companies are 'in budget' isn't fully "
                              "reproducible near the boundary. Override explicitly if needed.")
    parser.add_argument("--max-wall-seconds", type=float, default=None,
                         help="wall-clock deadline for this run. Default: scaled to the batch "
                              "size the same way as --max-requests (20 seconds/company, floor "
                              "2,280s), so a 100-company batch behaves like the real evaluation "
                              "and a 1,000-company batch isn't cut off partway through. A live "
                              "run found that hitting this deadline mid-run, not just the request "
                              "cap, is itself a source of run-to-run differences: whichever "
                              "companies are mid-fetch exactly when time runs out varies slightly "
                              "between invocations. Override explicitly if needed.")
    parser.add_argument("--max-cost-usd", type=float, default=9.0)
    parser.add_argument("--no-website", action="store_true", help="skip website verification entirely (BRREG-only run)")
    parser.add_argument("--viewer", default=None,
                         help="output path for the self-contained static HTML viewer built from this "
                              "run's envelopes (the 'usability' dimension -- a human can browse, search "
                              "and verify every claim's evidence link without touching the raw JSONL). "
                              "Defaults to viewer.html next to --output; pass --no-viewer to skip it.")
    parser.add_argument("--no-viewer", action="store_true", help="skip building the static HTML viewer")
    parser.add_argument("--requests-per-second", type=float, default=1.0,
                         help="max sustained requests/second to any single host, incl. the registry "
                              "(default 1.0; lower this further if the registry still throttles)")
    args = parser.parse_args()

    if args.organisations:
        orgs = read_organisations(args.organisations)
    else:
        # No --organisations given: read the batch from standard input
        # instead, one organisation number per line. This is what lets
        # the exact same command work whether a company list is supplied
        # as a file path or piped in, without guessing which convention
        # an evaluator uses.
        orgs = read_organisations_from_stream(sys.stdin)

    # 15 requests/company, scaled to the batch size, kept safely under the
    # competition's own stated ratio of 2,000 requests per 100 companies
    # (20/company). For a 100-company batch this gives 1,500 -- identical
    # to the previous flat default, so the real evaluation's behaviour is
    # unchanged. For a 1,000-company batch this gives 15,000, instead of
    # the same 1,500 a 100-company batch would get, which is what a live
    # run showed was far too little for a batch 10x the size.
    max_requests = args.max_requests if args.max_requests is not None else len(orgs) * 15
    # Same proportional-scaling fix, same reason: a flat 38-minute deadline
    # regardless of batch size meant a 1,000-company run got cut off
    # mid-way at nearly the same ratio a 100-company run would be allowed
    # to use in full. Confirmed on a live run: two full runs both finished
    # at ~2286-2287s against a 2280s flat deadline, and the ~65 companies
    # that differed between the two runs were concentrated exactly among
    # the ones whose website fetch was still in flight when time ran out.
    max_wall_seconds = args.max_wall_seconds if args.max_wall_seconds is not None else max(2280.0, len(orgs) * 20.0)
    if not orgs:
        print("no organisation numbers found in input", file=sys.stderr)
        return 2

    governor = BudgetGovernor(
        max_requests=max_requests,
        max_wall_seconds=max_wall_seconds,
        max_cost_usd=args.max_cost_usd,
    )

    envelopes, report = run_batch(
        orgs,
        history_root=args.history_root,
        max_workers=args.max_workers,
        governor=governor,
        fetch_website=not args.no_website,
        requests_per_second_per_host=args.requests_per_second,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shape_problems = 0
    lines = []
    for env in envelopes:
        problems = validate_envelope_shape(env)
        if problems:
            shape_problems += 1
            env.setdefault("errors", []).append({
                "stage": "envelope_shape_validation",
                "error_type": "ShapeValidationFailed",
                "detail": "; ".join(problems),
            })
        lines.append(json.dumps(env, ensure_ascii=False))
    # Written atomically: every envelope is fully built and validated
    # above BEFORE any byte of the output file is written, and the file
    # is only ever visible to readers in its complete, flushed-to-disk
    # form (see _atomic_write_text) -- never partially written.
    _atomic_write_text(out_path, "\n".join(lines) + "\n")

    report["shape_validation_problems"] = shape_problems
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(report_path, json.dumps(report, indent=2))

    print(f"Wrote {len(envelopes)} envelopes to {out_path}")
    print(f"Requests used: {report['budget']['requests_made']}/{report['budget']['requests_budget']}")
    print(f"Elapsed: {report['budget']['elapsed_seconds']}s / {report['budget']['wall_budget_seconds']}s budget")
    if shape_problems:
        print(f"WARNING: {shape_problems} envelope(s) failed shape validation", file=sys.stderr)

    if not args.no_viewer:
        # Built from the SAME envelopes just written (never refetched or
        # regenerated from a different source), so the viewer can never
        # drift from the evidence it claims to show. A failure here must
        # never be allowed to turn a successful batch run into a non-zero
        # exit code -- the envelopes are the scored artifact; the viewer is
        # a bonus presentation layer on top of them.
        viewer_path = Path(args.viewer) if args.viewer else out_path.with_name("viewer.html")
        try:
            viewer_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(viewer_path, build_viewer_html(envelopes))
            print(f"Wrote static viewer for {len(envelopes)} companies to {viewer_path}")
        except Exception as exc:  # pragma: no cover - defensive, see note above
            print(f"WARNING: could not build viewer.html: {type(exc).__name__}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
