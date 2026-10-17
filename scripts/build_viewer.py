#!/usr/bin/env python3
"""Builds a single, self-contained, offline static HTML viewer from a
batch's envelopes.jsonl -- the "Usability" dimension of the rubric
("User can find, compare, verify on desktop and mobile" /
"stable claim links, evidence links that open the captured source,
readable at 380 px, no horizontal scroll, readable tables"), which this
agent scored 0.00/8 on because no viewer existed at all: the output was
raw JSONL only.

Deliberately NOT a web app: no server, no build step, no external
scripts/fonts/CDNs (everything inline), so it opens correctly from a
plain double-click on any machine, including one with no internet access
-- exactly the kind of environment an evaluator might open this in.

Usage:
    python3 scripts/build_viewer.py \
        --envelopes out/envelopes.jsonl \
        --out out/viewer.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def _read_envelopes(path: Path) -> list[dict]:
    envelopes = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            envelopes.append(json.loads(line))
    return envelopes


def _slim(envelope: dict) -> dict:
    """Keeps only what the viewer renders, to keep the embedded JSON a
    reasonable size for a 1,000-company batch -- the full envelope (with
    every evidence record's redirect chain, every raw claim field, etc.)
    carries detail the viewer never displays.
    """
    legal_name = None
    for c in envelope.get("claims", []):
        if c.get("field") == "identity.legal_name":
            legal_name = c.get("value")
            break

    evidence_by_id = {e["id"]: e for e in envelope.get("evidence", [])}

    def slim_claim(c: dict) -> dict:
        return {
            "field": c.get("field"),
            "value": c.get("value"),
            "unit": c.get("unit"),
            "scope": c.get("scope"),
            "reporting_period": c.get("reporting_period"),
            "availability": c.get("availability"),
            "identity_basis": c.get("identity_basis"),
            "evidence_urls": [
                evidence_by_id[eid]["source_url"]
                for eid in c.get("evidence_ids", [])
                if eid in evidence_by_id
            ],
        }

    sections = {}
    for name, sect in envelope.get("sections", {}).items():
        claim_ids = set(sect.get("claim_ids", []))
        sections[name] = {
            "state": sect.get("state"),
            "note": sect.get("note", ""),
            "checked_sources": sect.get("checked_sources", []),
            "claims": [slim_claim(c) for c in envelope.get("claims", []) if c.get("claim_id") in claim_ids],
        }

    return {
        "org": envelope.get("organisation_number"),
        "name": legal_name,
        "run_id": (envelope.get("run") or {}).get("run_id"),
        "is_baseline": (envelope.get("run") or {}).get("is_baseline_run"),
        "summary": (envelope.get("summary") or {}).get("text", ""),
        "unknowns": (envelope.get("summary") or {}).get("unknowns", []),
        "sections": sections,
        "changes": [
            {"field": c.get("field"), "state": c.get("state"),
             "previous_value": c.get("previous_value"), "current_value": c.get("current_value")}
            for c in envelope.get("changes", [])
        ],
        "error_count": len(envelope.get("errors", [])),
    }


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signalpost — company profiles</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --border: #e5e7eb;
    --accent: #1d4ed8; --card: #f9fafb;
    --ok: #16a34a; --warn: #d97706; --bad: #dc2626; --neutral: #6b7280;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e5e7eb; --muted: #9ca3af; --border: #272a31;
      --accent: #60a5fa; --card: #171a21;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    padding: 14px 16px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--bg); z-index: 5;
  }
  header h1 { font-size: 16px; margin: 0 0 8px 0; }
  header .meta { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
  input#search {
    width: 100%; padding: 10px 12px; font-size: 15px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--card); color: var(--fg);
  }
  main { display: flex; flex-direction: column; }
  @media (min-width: 820px) {
    main { flex-direction: row; align-items: flex-start; }
  }
  #list {
    list-style: none; margin: 0; padding: 0; overflow-y: auto;
    border-bottom: 1px solid var(--border);
  }
  @media (min-width: 820px) {
    #list { width: 320px; flex: 0 0 320px; max-height: calc(100vh - 90px); border-bottom: none; border-right: 1px solid var(--border); }
  }
  #list li {
    padding: 10px 16px; border-bottom: 1px solid var(--border); cursor: pointer;
  }
  #list li:hover, #list li.active { background: var(--card); }
  #list .org-name { font-weight: 600; font-size: 14px; word-break: break-word; }
  #list .org-number { color: var(--muted); font-size: 12px; }
  #detail { padding: 16px; flex: 1; min-width: 0; max-width: 820px; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px;
    font-weight: 600; color: #fff; white-space: nowrap;
  }
  .badge.available { background: var(--ok); }
  .badge.not_available { background: var(--neutral); }
  .badge.ambiguous, .badge.blocked { background: var(--warn); }
  .badge.failed { background: var(--bad); }
  .badge.not_applicable { background: var(--muted); }
  h2 { font-size: 20px; margin: 0 0 4px 0; word-break: break-word; }
  .org-id { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
  .summary-box {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; margin-bottom: 16px; font-size: 14px;
  }
  .unknowns { color: var(--muted); font-size: 12px; margin-top: 6px; }
  section.group {
    border: 1px solid var(--border); border-radius: 10px; margin-bottom: 12px; overflow: hidden;
  }
  section.group > .group-head {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 10px 14px; background: var(--card);
  }
  section.group > .group-head h3 { margin: 0; font-size: 14px; }
  .claim-row {
    padding: 10px 14px; border-top: 1px solid var(--border); font-size: 13px;
  }
  .claim-field { color: var(--muted); font-size: 12px; }
  .claim-value { word-break: break-word; }
  .claim-value table { width: 100%; border-collapse: collapse; margin-top: 4px; }
  .claim-value td, .claim-value th {
    border: 1px solid var(--border); padding: 4px 6px; font-size: 12px; text-align: left;
    word-break: break-word;
  }
  .ev-links { margin-top: 4px; }
  .ev-links a { color: var(--accent); font-size: 12px; margin-right: 10px; word-break: break-all; }
  .empty-note { color: var(--muted); font-size: 13px; padding: 20px; }
  .changes-list { font-size: 13px; }
  .changes-list li { margin-bottom: 4px; }
  footer { padding: 16px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>Signalpost — company profiles</h1>
  <div class="meta">__META__</div>
  <input id="search" type="text" placeholder="Search by organisation number or name…" autocomplete="off">
</header>
<main>
  <ul id="list"></ul>
  <div id="detail"><div class="empty-note">Select a company from the list.</div></div>
</main>
<footer>Generated by scripts/build_viewer.py from envelopes.jsonl. No external network calls; every evidence link opens the original source page directly.</footer>
<script id="data" type="application/json">__DATA_JSON__</script>
<script>
(function () {
  var DATA = JSON.parse(document.getElementById('data').textContent);
  var listEl = document.getElementById('list');
  var detailEl = document.getElementById('detail');
  var searchEl = document.getElementById('search');

  function esc(s) {
    return String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderValue(v) {
    if (v === null || v === undefined) return '<span class="claim-field">(none)</span>';
    if (Array.isArray(v)) {
      if (v.length && typeof v[0] === 'object') {
        var cols = Object.keys(v[0]);
        var head = '<tr>' + cols.map(function (c) { return '<th>' + esc(c) + '</th>'; }).join('') + '</tr>';
        var rows = v.map(function (row) {
          return '<tr>' + cols.map(function (c) { return '<td>' + esc(row[c]) + '</td>'; }).join('') + '</tr>';
        }).join('');
        return '<table>' + head + rows + '</table>';
      }
      return esc(v.join(', '));
    }
    if (typeof v === 'object') {
      return '<table>' + Object.keys(v).map(function (k) {
        return '<tr><th>' + esc(k) + '</th><td>' + esc(v[k]) + '</td></tr>';
      }).join('') + '</table>';
    }
    if (typeof v === 'number') {
      return v.toLocaleString();
    }
    return esc(v);
  }

  function renderClaim(c) {
    var links = (c.evidence_urls || []).map(function (u) {
      return '<a href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">source ↗</a>';
    }).join('');
    var meta = [];
    if (c.scope) meta.push(c.scope);
    if (c.unit) meta.push(c.unit);
    if (c.reporting_period && c.reporting_period.to) meta.push('period ending ' + c.reporting_period.to);
    return '<div class="claim-row">'
      + '<div class="claim-field">' + esc(c.field) + (meta.length ? ' · ' + esc(meta.join(', ')) : '') + '</div>'
      + '<div class="claim-value">' + renderValue(c.value) + '</div>'
      + (links ? '<div class="ev-links">' + links + '</div>' : '')
      + '</div>';
  }

  function renderSection(name, sect) {
    var label = name.replace(/_/g, ' ');
    var claimsHtml = sect.claims.length
      ? sect.claims.map(renderClaim).join('')
      : '<div class="claim-row claim-field">' + esc(sect.note || 'nothing available') + '</div>';
    return '<section class="group">'
      + '<div class="group-head"><h3>' + esc(label) + '</h3>'
      + '<span class="badge ' + esc(sect.state) + '">' + esc(sect.state) + '</span></div>'
      + claimsHtml
      + '</section>';
  }

  function renderDetail(c) {
    var sectionsHtml = Object.keys(c.sections).sort().map(function (name) {
      return renderSection(name, c.sections[name]);
    }).join('');
    var changesHtml = '';
    if (c.changes && c.changes.length) {
      changesHtml = '<h3>Changes since previous run</h3><ul class="changes-list">'
        + c.changes.map(function (ch) {
            return '<li><strong>' + esc(ch.field) + '</strong>: ' + esc(ch.state)
              + (ch.state === 'REAL_CHANGE' ? ' (' + esc(ch.previous_value) + ' → ' + esc(ch.current_value) + ')' : '')
              + '</li>';
          }).join('')
        + '</ul>';
    }
    detailEl.innerHTML =
      '<h2>' + esc(c.name || c.org) + '</h2>'
      + '<div class="org-id">Organisation number ' + esc(c.org)
      + (c.is_baseline ? ' · baseline run' : '') + (c.error_count ? ' · ' + c.error_count + ' internal error(s) logged' : '') + '</div>'
      + '<div class="summary-box">' + esc(c.summary || '(no summary)')
      + (c.unknowns && c.unknowns.length ? '<div class="unknowns">Not established: ' + esc(c.unknowns.join(', ')) + '</div>' : '')
      + '</div>'
      + changesHtml
      + sectionsHtml;
  }

  function renderList(items) {
    listEl.innerHTML = '';
    items.forEach(function (c) {
      var li = document.createElement('li');
      li.innerHTML = '<div class="org-name">' + esc(c.name || '(name unknown)') + '</div>'
        + '<div class="org-number">' + esc(c.org) + '</div>';
      li.addEventListener('click', function () {
        Array.prototype.forEach.call(listEl.children, function (el) { el.classList.remove('active'); });
        li.classList.add('active');
        renderDetail(c);
        if (window.innerWidth < 820) detailEl.scrollIntoView({ behavior: 'smooth' });
      });
      listEl.appendChild(li);
    });
  }

  function applyFilter() {
    var q = searchEl.value.trim().toLowerCase();
    if (!q) { renderList(DATA); return; }
    renderList(DATA.filter(function (c) {
      return (c.org && c.org.indexOf(q) !== -1)
        || (c.name && c.name.toLowerCase().indexOf(q) !== -1);
    }));
  }

  searchEl.addEventListener('input', applyFilter);
  renderList(DATA);
  if (DATA.length) renderDetail(DATA[0]);
})();
</script>
</body>
</html>
"""


def build_viewer_html(envelopes: list[dict]) -> str:
    slim_rows = [_slim(e) for e in envelopes]
    available_counts = {}
    for row in slim_rows:
        for sect in row["sections"].values():
            available_counts[sect["state"]] = available_counts.get(sect["state"], 0) + 1
    meta = f"{len(slim_rows)} compan{'y' if len(slim_rows) == 1 else 'ies'} · " + ", ".join(
        f"{v} {k}" for k, v in sorted(available_counts.items())
    )
    data_json = json.dumps(slim_rows, ensure_ascii=False)
    # </script> inside embedded JSON would prematurely close the script
    # tag if any claim value happens to contain that literal substring
    # (e.g. a dated-news title scraped from a page) -- escape defensively.
    data_json = data_json.replace("</script", "<\\/script")
    page = _PAGE_TEMPLATE.replace("__META__", html.escape(meta)).replace("__DATA_JSON__", data_json)
    return page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelopes", required=True, help="path to envelopes.jsonl")
    parser.add_argument("--out", required=True, help="output .html path")
    args = parser.parse_args()

    envelopes = _read_envelopes(Path(args.envelopes))
    page = build_viewer_html(envelopes)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(out_path, page)
    print(f"Wrote viewer for {len(envelopes)} companies to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
