"""Claim/evidence schema shared by every module that produces a fact.

Design (see EVIDENCE_ARCHITECTURE.md):
  * A Claim is a typed, canonicalised fact about one company.
  * An Evidence record points to a stored snapshot (raw bytes) plus a
    locator into it, and a `span` that must literally appear in the
    decoded bytes -- this is the anti-hallucination check.
  * claim_id is a deterministic hash of (org, field, canonical value,
    period, source_key) so the SAME real-world fact always gets the SAME
    id across separate runs. This is what makes refresh possible without
    duplicates (see refresh.py).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

AVAILABILITY_STATES = (
    "available", "not_available", "blocked", "not_applicable", "ambiguous", "failed",
)

SOURCE_CLASSES = (
    "official_registry", "official_filing", "official_feed",
    "company_owned", "company_linked_third_party", "third_party_public",
)


def canonicalize_text(value: str) -> str:
    """NFC-normalise and collapse whitespace. Used for both string values
    and for decoded page text before span-matching, so a span found in a
    canonicalised text is still a real substring relationship.
    """
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u00a0", " ")  # non-breaking space
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonicalize_value(value: Any) -> Any:
    """Canonical form used inside a claim_id hash. Keep this stable --
    changing it changes every claim_id ever produced.
    """
    if isinstance(value, str):
        return canonicalize_text(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (list, tuple)):
        return [canonicalize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: canonicalize_value(v) for k, v in sorted(value.items())}
    return value


@dataclass
class Evidence:
    id: str
    source_url: str
    final_url: str
    source_class: str                    # one of SOURCE_CLASSES
    retrieved_at: str                    # ISO-8601 UTC
    http_status: Optional[int]
    content_sha256: Optional[str]
    snapshot_path: Optional[str]         # relative path under the snapshot root
    locator: dict                        # e.g. {"json_pointer": "..."} or {"selector": "...", "char_range": [a,b]}
    claim_span: Optional[str]            # verbatim substring this evidence supports
    extractor: str                       # e.g. "brreg_accounts@1.0"
    redirect_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Claim:
    claim_id: str
    organisation_number: str
    field: str                    # dotted path, e.g. "accounts.revenue"
    value: Any
    unit: Optional[str]
    scope: Optional[str]          # e.g. "SELSKAP" | "KONSERN" | None
    reporting_period: Optional[dict]   # {"from": "...", "to": "..."} or None
    availability: str             # one of AVAILABILITY_STATES
    identity_basis: Optional[str]  # e.g. "K2:brreg_key", "K1:footer_org_number"
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def make_claim_id(
    organisation_number: str,
    field_name: str,
    value: Any,
    *,
    scope: Optional[str] = None,
    period: Optional[dict] = None,
    source_key: str = "",
) -> str:
    """Deterministic id: same real-world fact -> same id, every run."""
    payload = {
        "org": organisation_number,
        "field": field_name,
        "value": canonicalize_value(value),
        "scope": scope,
        "period": period,
        "source_key": source_key,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def field_key(organisation_number: str, field_name: str, *, scope: Optional[str] = None,
              period: Optional[dict] = None) -> str:
    """Identity of 'this slot' independent of its value -- used by refresh
    to detect that the SAME field now has a DIFFERENT value (a REAL_CHANGE),
    as opposed to a brand-new field (NEW). See refresh.py.
    """
    payload = {"org": organisation_number, "field": field_name, "scope": scope, "period": period}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def verify_span(decoded_text: str, span: str) -> bool:
    """True iff `span` is a verbatim substring of `decoded_text` after both
    are canonicalised the same way. This is the core anti-hallucination
    check: nothing may be published from unstructured text unless the exact
    words backing it can be found in the bytes we actually fetched.
    """
    if not span:
        return False
    return canonicalize_text(span) in canonicalize_text(decoded_text)


class ClaimValidationError(Exception):
    pass


def validate_claim(claim: Claim, evidence_by_id: dict[str, Evidence]) -> list[str]:
    """Returns a list of validation problems (empty list = valid). Never
    raises; callers decide whether to drop the claim or just log it.
    Mirrors the acceptance checks in EVIDENCE_ARCHITECTURE.md section 7.
    """
    problems: list[str] = []

    if claim.availability not in AVAILABILITY_STATES:
        problems.append(f"invalid availability state: {claim.availability!r}")

    if claim.availability == "available":
        if claim.value is None:
            problems.append("availability=available but value is None")
        if not claim.evidence_ids:
            problems.append("availability=available but no evidence_ids")
        for eid in claim.evidence_ids:
            ev = evidence_by_id.get(eid)
            if ev is None:
                problems.append(f"evidence_id {eid} not found")
                continue
            if ev.source_class not in SOURCE_CLASSES:
                problems.append(f"evidence {eid} has invalid source_class {ev.source_class!r}")
            if not ev.source_url or not ev.retrieved_at:
                problems.append(f"evidence {eid} missing source_url/retrieved_at")
            # Structured (JSON-keyed) sources are exempt from span-checking;
            # anything else must carry a verified span.
            if ev.locator.get("json_pointer") is None and not ev.claim_span:
                problems.append(f"evidence {eid} is text-derived but has no claim_span")

        if claim.field.startswith("accounts.") and claim.scope is None:
            problems.append("financial claim missing required scope (SELSKAP/KONSERN)")
        if claim.field.startswith("accounts.") and claim.reporting_period is None:
            problems.append("financial claim missing required reporting_period")

    return problems
