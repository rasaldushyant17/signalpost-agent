# Limitations (v1)

Read this before assuming a gap is a bug rather than a known, deliberate
scope decision. The reasoning behind each cut is in the accompanying
analysis documents from earlier in this project (IDENTITY_SAFETY.md,
COVERAGE_STRATEGY.md, RESOURCE_BUDGET.md, IMPLEMENTATION_ROADMAP.md) if
you have them; the short version is below.

## Built and tested in this version

- Legal identity, accounts (with SELSKAP/KONSERN scope and reporting
  period preserved), leadership roles, registered subunits — all from
  Brønnøysundregistrene (BRREG), keyed by organisation number.
- Registry-declared website verification, using a conservative,
  contextual organisation-number identity gate (never name-similarity
  alone). See `src/signalpost_agent/identity.py`.
- **Independent website discovery** (`src/signalpost_agent/discovery.py`)
  for the majority of companies with no registry-declared site, and as a
  fallback when a declared site is unreachable or fails the identity
  check: a small, deterministic set of candidate domains is generated
  from the company's own registered legal name (no search engine, no
  paid API), and each candidate still has to pass the exact same
  contextual organisation-number identity gate as a registry-declared
  site before anything is published. A wrong guess costs one wasted
  fetch, never a wrong-company publication.
- Company-owned **social/profile links** (e.g. LinkedIn), published only
  as a reciprocal outbound link found on an already-verified official
  site (tier L) -- never independently crawled or trusted on the
  platform itself.
- Job postings and dated activity items extracted from a *verified*
  site's careers/news pages, structured data only (JSON-LD), never
  guessed from prose.
- Deterministic, canonicalised refresh with append-only history: a fetch
  failure is never reported as a change, list re-ordering is never
  reported as a change, and prior evidence is never overwritten.
- A template-only summary with a numeric-containment check — it will
  never state a number that isn't backed by an available claim. Covers
  identity, accounts, leadership, address, subunits, verified website,
  social profiles, job postings, dated activity, and refresh changes
  (each sentence only appears if its backing claim is actually
  available), plus an explicit "not established from checked sources"
  line listing what genuinely wasn't found.
- A self-contained, offline static HTML viewer (`scripts/build_viewer.py`,
  wired automatically into `run_batch.py`) — searchable, one page per
  company, every claim linked to its evidence source, mobile-safe, no
  external dependencies. Previously the only output was raw JSONL with
  no way to browse or verify it.
- Crash isolation at every level: one company's failure (even a raw,
  unexpected exception) never reduces the batch's envelope count below
  exactly one envelope per input.
- A real, deterministic 1,000-company submission manifest built from the
  official universe file, stratified to include the hard cases (namesake
  companies, shared website hosts, non-AS legal forms, bankrupt/regulated
  industries, unusual characters) rather than avoiding them.

## Deliberately out of scope for v1

- **NAV job-ad feed integration.** The only source that would give
  hiring facts a fully structural (non-identity-risky) key for companies
  without their own careers page. Not built because: (a) this sandbox
  cannot reach NAV's API to test it, and (b) its feasibility (index-build
  cost, token/terms, actual coverage) was never measured. The module
  layout leaves room for it; adding it is the highest-value next step if
  you can test it against the live feed yourself.
- **Search-engine-based website discovery.** Discovery is now built (see
  above), but deliberately as deterministic domain-candidate guessing
  from the company's own legal name rather than a search-engine query:
  this agent's declared source policy is BRREG data plus companies' own
  public websites, with no paid third-party API and no search-engine
  scraping of uncertain legal/terms standing. This bounds recall (a
  company whose real domain bears no resemblance to its legal name will
  not be found), which is the known, accepted trade-off of this
  approach; a measured, permitted search API would recover more of
  those cases but was out of scope for this pass.
- **Annual-report PDF text/OCR extraction** for employee counts (only
  ~14.5% of companies in the real universe file have a registry-reported
  employee count — see below). Needs `pdftoppm`/`tesseract` availability
  in the run environment, which wasn't confirmed.
- **LinkedIn, Meta, and similar platforms** are not used anywhere, by
  design — the source policy explicitly restricts them, and this
  agent never scrapes them, guest-endpoint or otherwise.

## A genuine property of the real universe file, not a bug

Running the sampler against the actual 411,160-row
`signalpost-company-universe-2025_jsonl.gz` found:
- **10.91%** of companies have any registry-declared website at all.
- **14.55%** have any registry-reported employee count.
- **91.68%** are plain `AS` companies.
- **Zero** companies in this eligible universe are flagged
  `bankrupt` or `liquidating` — the "frozen 2025-filer" eligibility rule
  appears to exclude them already, so the "S10_bankrupt_or_liquidating"
  stratum in the manifest is empty by design of the source data, not a
  sampling bug. (Verified directly against the file; see the composition
  report for the exact count.)

This means: for a random daily batch of 100 companies, only about 11 had
a registry-declared site to check before discovery.py existed. With
discovery now built, the other ~89% get a bounded set of deterministic
domain guesses from their own legal name instead of nothing -- but not
every company's real domain resembles its legal name, so this still will
not reach all of them. Where no candidate verifies, the agent says so
honestly (`not_available`) rather than inventing one — this remains a
deliberate trade-off in favour of never publishing a wrong-company fact,
at the cost of lower website-family coverage than an agent willing to
guess without the same identity proof.

## What could not be executed from inside this build environment

The sandbox this code was built and tested in has network access to a
small allow-list of developer domains (PyPI, npm, GitHub, etc.) and
**cannot reach `data.brreg.no` or NAV's job-ad API**. This was confirmed
by direct testing during development (an actual request to BRREG
returned a proxy-level block, not a BRREG error). Consequences:

- Every module that talks to BRREG or a company website is tested
  against realistic, hand-built fixtures (see `tests/`) that mirror the
  real API's JSON shape and known edge cases (deleted entities, KONSERN
  vs SELSKAP accounts, truncated HTTP bodies, wrong-charset pages,
  group-listing pages, redirects to a different domain, and so on) —
  109 tests, all passing.
- **No live company profile has been generated by this build.** The
  ">=1,000 completed profiles" submission requirement is not yet
  satisfied — only the manifest of which 1,000 organisation numbers to
  research is done (that part needed no network access, since the real
  universe file was provided directly).
- To produce the actual profiles: run `scripts/run_batch.py` (see
  README.md) from a machine or environment with normal internet access.
  Nothing else needs to change; the code doesn't know it's running
  somewhere different.

## Honest self-assessment against the official-run checks

| Check | Status |
|---|---|
| No fabricated financial values / wrong-company publication | Designed and tested against every reproduced failure mode found in an earlier audit of a reference implementation; not proven against a large *live*, blind-labelled sample (that requires a real run) |
| >=1,000 completed profiles + manifest | Manifest: done. Profiles: **not done** — requires a live run outside this sandbox |
| Exactly 100 terminal envelopes per daily batch | Guaranteed by construction and tested under fault injection (see `tests/test_batch.py`); not yet proven against a real 100-company live batch |
| Claim-level source, retrieval time, reporting period | Built into the schema and enforced by a validator that drops non-conforming claims rather than publishing them |
| Missing values never silently zero | Enforced throughout; every section always resolves to one of the six honest states |
| Idempotent refresh, prior snapshots preserved | Tested (see `tests/test_refresh.py`); the append-only history store never overwrites |
| Reproducible setup, pinned deps, one evaluator command | `pyproject.toml` pins exact versions; `scripts/run_batch.py` is the one command |
| Source rights, secrets, safe URL handling | No API keys required anywhere in v1 (BRREG is open data); SSRF/private-IP guard built into the HTTP client |
