"""Assembles the terminal envelope for one organisation number, matching
the field names in the official output contract (evaluation contract:
"Envelopes must contain legal identity, claims, evidence references,
availability states, source snapshots, refresh metadata and errors") and
the brief's 7 required sections.

Exactly one envelope per input, ALWAYS -- even when every source failed.
This is what fixes the reproduced "one exception kills the whole batch"
bug: pipeline.py guarantees this function is reached for every company by
catching everything upstream (see pipeline.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .claims import Claim, Evidence, AVAILABILITY_STATES
from .refresh import ChangeRecord

SECTION_NAMES = (
    "legal_identity_and_brand",
    "accounts_and_history",
    "leadership_and_workplaces",
    "website_and_company_profiles",
    "hiring_and_dated_activity",
    "claim_level_evidence",
    "refresh_metadata",
)

_FIELD_TO_SECTION = {
    "identity.legal_name": "legal_identity_and_brand",
    "identity.legal_form_code": "legal_identity_and_brand",
    "identity.legal_form_description": "legal_identity_and_brand",
    "identity.registration_date": "legal_identity_and_brand",
    "identity.industry_code": "legal_identity_and_brand",
    "identity.industry_description": "legal_identity_and_brand",
    "identity.registry_declared_website": "legal_identity_and_brand",
    "identity.bankrupt": "legal_identity_and_brand",
    "identity.under_liquidation": "legal_identity_and_brand",
    "identity.dissolved_date": "legal_identity_and_brand",
    "identity.parent_organisation_number": "legal_identity_and_brand",
    "identity.employees_registered": "legal_identity_and_brand",
    "identity.public_brand": "legal_identity_and_brand",
    "accounts.revenue": "accounts_and_history",
    "accounts.annual_result": "accounts_and_history",
    "leadership.roles": "leadership_and_workplaces",
    "workplace.business_address": "leadership_and_workplaces",
    "workplace.subunits": "leadership_and_workplaces",
    "website.official": "website_and_company_profiles",
    "website.company_profile": "website_and_company_profiles",
    "website.social_profile": "website_and_company_profiles",
    "hiring.job_posting": "hiring_and_dated_activity",
    "activity.dated_item": "hiring_and_dated_activity",
}


def section_for_field(field_name: str) -> str:
    if field_name in _FIELD_TO_SECTION:
        return _FIELD_TO_SECTION[field_name]
    prefix = field_name.split(".", 1)[0]
    return {
        "identity": "legal_identity_and_brand",
        "accounts": "accounts_and_history",
        "leadership": "leadership_and_workplaces",
        "workplace": "leadership_and_workplaces",
        "website": "website_and_company_profiles",
        "hiring": "hiring_and_dated_activity",
        "activity": "hiring_and_dated_activity",
    }.get(prefix, "claim_level_evidence")


@dataclass
class SectionState:
    state: str                      # one of AVAILABILITY_STATES
    claim_ids: list[str] = field(default_factory=list)
    checked_sources: list[str] = field(default_factory=list)
    note: str = ""


def section_state_from_claims(claims: list[Claim], checked_sources: list[str],
                               *, empty_state: str = "not_available") -> SectionState:
    """Rolls a set of claims for one section into one section-level state.
    `empty_state` lets a caller distinguish "checked, genuinely nothing"
    (not_available) from other honest outcomes when there were no claims
    at all but a specific failure/block reason is known (pass that instead).
    """
    available = [c for c in claims if c.availability == "available"]
    if available:
        return SectionState(state="available", claim_ids=[c.claim_id for c in available],
                             checked_sources=checked_sources)
    non_available_states = {c.availability for c in claims}
    if "ambiguous" in non_available_states:
        return SectionState(state="ambiguous", checked_sources=checked_sources)
    if "blocked" in non_available_states:
        return SectionState(state="blocked", checked_sources=checked_sources)
    if "failed" in non_available_states:
        return SectionState(state="failed", checked_sources=checked_sources)
    if "not_applicable" in non_available_states:
        return SectionState(state="not_applicable", checked_sources=checked_sources)
    return SectionState(state=empty_state, checked_sources=checked_sources,
                         note="checked; nothing found (not the same as zero)")


def build_envelope(
    *,
    organisation_number: str,
    run_id: str,
    started_at: str,
    completed_at: str,
    sections: dict[str, SectionState],
    claims: list[Claim],
    evidence: dict[str, Evidence],
    changes: list[ChangeRecord],
    errors: list[dict],
    operations: dict,
    is_baseline_run: bool,
) -> dict:
    """Builds ONE terminal envelope. Never raises -- any exception in a
    caller's data should have been converted to an `errors` entry before
    this is called (pipeline.py enforces that boundary).
    """
    for name in SECTION_NAMES:
        sections.setdefault(name, SectionState(state="not_available", note="section not attempted"))

    return {
        "organisation_number": organisation_number,
        "run": {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "terminal_status": "completed",
            "is_baseline_run": is_baseline_run,
        },
        "sections": {
            name: {
                "state": s.state,
                "claim_ids": s.claim_ids,
                "checked_sources": s.checked_sources,
                "note": s.note,
            }
            for name, s in sections.items()
        },
        "claims": [c.to_dict() for c in claims],
        "evidence": [e.to_dict() for e in evidence.values()],
        "changes": [
            {
                "field": c.field, "state": c.state, "field_key": c.field_key,
                "previous_claim_id": c.previous_claim_id, "current_claim_id": c.current_claim_id,
                "previous_value": c.previous_value, "current_value": c.current_value,
                "note": c.note,
            }
            for c in changes
        ],
        "errors": errors,
        "operations": operations,
    }


def validate_envelope_shape(envelope: dict) -> list[str]:
    """Structural sanity check (mirrors the kind of check Builderr's public
    citation/output validator performs -- see IMPLEMENTATION_ROADMAP E9).
    This is NOT a scoring function; it just catches shape bugs before they
    reach a real submission.
    """
    problems = []
    if not envelope.get("organisation_number"):
        problems.append("missing organisation_number")
    for name in SECTION_NAMES:
        sect = envelope.get("sections", {}).get(name)
        if sect is None:
            problems.append(f"missing required section {name!r}")
            continue
        if sect.get("state") not in AVAILABILITY_STATES:
            problems.append(f"section {name!r} has invalid state {sect.get('state')!r}")
    claim_ids = {c["claim_id"] for c in envelope.get("claims", [])}
    evidence_ids = {e["id"] for e in envelope.get("evidence", [])}
    for c in envelope.get("claims", []):
        if c.get("availability") == "available":
            for eid in c.get("evidence_ids", []):
                if eid not in evidence_ids:
                    problems.append(f"claim {c['claim_id']} references missing evidence {eid}")
    return problems
