# Signalpost Agent

A Norwegian company research agent built for the Builderr Signalpost
challenge. Given an organisation number, it returns a claim-level company
profile with evidence, honest availability states, and idempotent refresh
— designed around the exact failure modes found in an earlier engineering
audit of a reference implementation (see `LIMITATIONS.md` for what that
means in practice).

**Read `LIMITATIONS.md` first.** It says plainly what is and isn't done,
and why.

## Requirements

- Python 3.12+
- Outbound network access to `data.brreg.no` (official Norwegian company
  registry) and, per company, that company's own verified website. No API
  key is required for either.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
pip install -e ".[dev]"            # only needed to run the test suite
```

## Run the tests

```bash
pytest -q
```

All 100+ tests run with no network access (everything is tested against
in-memory fixtures matching BRREG's real JSON shape).

## Build the submission manifest

Already done once against the real universe file and committed under
`manifest/`. To rebuild it (e.g. with a different seed, or against a
refreshed universe file):

```bash
python3 scripts/build_manifest.py \
    --universe /path/to/signalpost-company-universe-2025.jsonl.gz \
    --out manifest \
    --seed signalpost-agent-v1
```

This is deterministic: the same universe file and seed always produce the
same 1,000 organisation numbers (proven by `tests/test_sampling.py`), and
the run prints a SHA-256 of the exact selection so it can be checked.

## Run a batch (the one evaluator command)

```bash
python3 scripts/run_batch.py \
    --organisations manifest/organisation_numbers.txt \
    --history-root state/history \
    --output out/envelopes.jsonl \
    --report out/run-report.json
```

- `--organisations` accepts either a plain text file (one organisation
  number per line) or a `.jsonl` file with an `organisation_number` field
  per line (the manifest builder writes both). **If `--organisations` is
  omitted entirely, the batch is read from standard input instead** (one
  organisation number per line). This matters: Builderr's current rules
  say they supply the official company batch at run time and run your
  command against it directly -- they do not score a precomputed profile
  file. If the submitted run command only ever pointed at a fixed local
  file, an evaluator supplying a different batch some other way (e.g.
  piping it in) would never actually reach your agent, which would show
  up as a near-zero recall score despite the agent working correctly in
  your own testing. Supporting both a file path and stdin is a defence
  against not knowing exactly which convention an evaluator uses.
- `--history-root` is a directory of append-only per-company history
  files, used to detect real changes on the next run. Omit it to run a
  one-off baseline with no refresh comparison.
- Output is one JSON object per line in `--output`, exactly one per input
  organisation number, always — including when every source for a
  company failed.
- `--report` gets a JSON summary: request count, elapsed time, and any
  exceptions that had to be caught at the outermost safety net (should
  always be zero; see `tests/test_batch.py`).
- A self-contained, offline static HTML viewer is also written next to
  `--output` (default `viewer.html`; override with `--viewer <path>`, or
  skip it with `--no-viewer`). It's built straight from the same
  envelopes the run just produced — searchable by name/organisation
  number, one click per company, every claim shown with a direct link to
  its evidence source, mobile-safe, no server, no external scripts/fonts,
  works from a plain double-click with no internet access. See
  `scripts/build_viewer.py`.

Budget flags (defaults are conservative relative to the competition's
locked limits, to leave margin for retries/redirects the request count
doesn't obviously show):

```bash
--max-workers 8             # concurrent companies
--max-requests (auto)       # defaults to 15 x however many companies you passed in, which
                             # gives 1,500 for a 100-company batch (same ratio as the
                             # competition's own 2,000-per-100-companies rule) and scales up
                             # correctly for larger batches -- override explicitly if you want
--max-wall-seconds 2280     # 38 minutes; hard cap is 45 minutes
--max-cost-usd 9.0          # hard cap is $10; v1 makes zero paid API calls
--no-website                 # BRREG-only run, skips all website/identity work
```

### Checking the run is reproducible

Before trusting or submitting any output, run the exact same command twice
(to two different output files) and compare them:

```bash
python3 scripts/run_batch.py --organisations manifest/organisation_numbers.txt \
    --output out/run1.jsonl --report out/run1-report.json

python3 scripts/run_batch.py --organisations manifest/organisation_numbers.txt \
    --output out/run2.jsonl --report out/run2-report.json

python3 scripts/verify_determinism.py out/run1.jsonl out/run2.jsonl
```

This reports `READY` only if the two runs found exactly the same facts for
every company (it correctly ignores the parts that are *supposed* to
differ between runs, like real fetch timestamps). If it reports
`NOT READY`, something is still non-deterministic and should be fixed
before submitting.

### Refreshing (running it again later)

Run the exact same command again with the same `--history-root`. Each
company's new claims are diffed against its own append-only history:
unchanged facts are reported `NO_CHANGE`, new facts `NEW`, altered facts
`REAL_CHANGE` (with both the old and new value visible), and anything
that couldn't be re-checked this time is `SOURCE_UNAVAILABLE` or
`BLOCKED` — never silently treated as a deletion.

## What each part does

| Module | Responsibility |
|---|---|
| `org_number.py` | Norwegian organisation-number checksum validation and contextual extraction from text |
| `http_client.py` | Crash-safe HTTP fetching: typed results for every failure mode, allow-listed retries, SSRF guard |
| `registry_brreg.py` | Brønnøysundregistrene connector: identity, accounts (scope + period preserved), roles, subunits |
| `identity.py` | Decides whether a crawled page really is the target company's site — conservative by design |
| `discovery.py` | Generates deterministic candidate domains from a company's own legal name, for the majority of companies with no registry-declared website (or where that site fails the identity check) — no search engine, no paid API |
| `website.py` | Fetches and parses company websites: charset detection, redirect/domain checking, structured data extraction |
| `claims.py` | The claim/evidence schema, deterministic claim IDs, and the anti-hallucination span check |
| `refresh.py` | The refresh state machine and append-only claim history |
| `budget.py` | Wall-clock deadline, real request accounting, per-host circuit breaker |
| `summary.py` | Template-only synthesis with a numeric-containment safety check (no LLM) |
| `envelope.py` | Assembles the final terminal envelope, one per company |
| `pipeline.py` | Runs one company end-to-end; guarantees an envelope is always produced |
| `batch.py` | Runs many companies concurrently; guarantees exactly N envelopes for N inputs |
| `sampling.py` | Builds the stratified submission manifest from the universe file |
| `scripts/build_viewer.py` | Builds a self-contained, offline static HTML viewer from a run's envelopes — the usability layer, wired automatically into `run_batch.py` |

## Models, APIs, and cost

No LLM calls anywhere in this version (see `LIMITATIONS.md` for why, and
what would need to be true before adding one). No paid third-party API
calls, and no search-engine scraping: website discovery (`discovery.py`)
guesses candidate domains from the company's own legal name rather than
querying a search engine. The only external services ever fetched are
`data.brreg.no` (official, free, no key), each company's own website
(free, no key), and — only while trying to verify a guessed domain — a
small number of plain, unauthenticated HTTPS requests to those candidate
domains, never published unless they pass the same identity check as a
registry-declared site. Declared third-party API spend per 100-company
batch: **$0**.
