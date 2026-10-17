"""Connector for Brønnøysundregistrene (BRREG), the Norwegian Central
Coordinating Register. This is the identity anchor for every profile
(source policy: "Official data is the identity anchor. It does not, by
itself, identify the public brand or website.").

Every fact here is K2 by construction: the record is keyed by the
organisation number we asked for, so no name-matching is involved. The
only identity check needed is confirming the response's own
`organisasjonsnummer` field equals what we requested.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .claims import Claim, Evidence, make_claim_id, canonicalize_text
from .http_client import BudgetedFetcher, FetchResult

BASE = "https://data.brreg.no"

ENDPOINTS = {
    "entity": "{base}/enhetsregisteret/api/enheter/{org}",
    "sub_entity": "{base}/enhetsregisteret/api/underenheter/{org}",  # tried if 'entity' 404s (some numbers are sub-units)
    "accounts": "{base}/regnskapsregisteret/regnskap/{org}",
    "roles": "{base}/enhetsregisteret/api/enheter/{org}/roller",
    "subunits": "{base}/enhetsregisteret/api/underenheter?overordnetEnhet={org}",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ModuleResult:
    availability: str          # "available" | "not_available" | "blocked" | "failed" | "not_applicable"
    claims: list[Claim]
    evidence: dict[str, Evidence]
    raw: Optional[dict] = None
    note: str = ""


def _evidence_from_fetch(evidence_id: str, url: str, result: FetchResult, *,
                          source_class: str, extractor: str, locator: dict,
                          claim_span: Optional[str] = None) -> Evidence:
    return Evidence(
        id=evidence_id, source_url=url, final_url=result.final_url,
        source_class=source_class, retrieved_at=now_iso(),
        http_status=result.status, content_sha256=result.content_sha256,
        snapshot_path=None,  # filled in by the snapshot store when persisted
        locator=locator, claim_span=claim_span, extractor=extractor,
        redirect_chain=result.redirect_chain,
    )


def _get_registry_with_404_retry(url: str, fetcher: BudgetedFetcher, *,
                                  retries: int = 3, backoff: float = 4.0) -> FetchResult:
    """Deliberately narrow, BRREG-only exception to the normal rule that a
    404 is trusted immediately.

    On a live 1,000-company run (2026-09-30), ~64% of lookups against the
    real registry came back as a plain HTTP 404 for organisation numbers
    that, re-checked individually afterwards, resolved correctly with real
    data every time -- evidence pointed to a sustained-rate limit (see
    http_client.py's _TokenBucket, the primary fix) rather than a brief
    blip, so the backoff here is deliberately patient (several seconds,
    not a few hundred milliseconds) rather than a quick micro-retry: a
    short retry during an active throttling window just hits the same
    wall again. We have independent evidence (the organisation number
    came from the official, frozen eligible-universe file) that a
    registry 404 is more likely to be throttling than a genuine absence.
    This reasoning does NOT apply to a company's own website, where a 404
    usually means exactly what it says -- that 404 handling is untouched.
    """
    result = fetcher.get(url, accept="application/json")
    attempt = 0
    while result.status == 404 and attempt < retries:
        time.sleep(backoff * (attempt + 1))
        result = fetcher.get(url, accept="application/json")
        attempt += 1
    return result


def _availability_for_fetch(result: FetchResult) -> str:
    if result.ok:
        return "available"
    if result.status == 404:
        return "not_available"
    if result.status == 410:
        return "not_applicable"
    if result.error_kind == "blocked_policy":
        return "blocked"
    return "failed"


def fetch_entity(org: str, fetcher: BudgetedFetcher) -> ModuleResult:
    url = ENDPOINTS["entity"].format(base=BASE, org=org)
    result = _get_registry_with_404_retry(url, fetcher)
    avail = _availability_for_fetch(result)
    if avail != "available":
        return ModuleResult(availability=avail, claims=[], evidence={},
                             note=f"{result.error_kind or result.status}: {result.error_detail}")

    import json
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except Exception as exc:
        return ModuleResult(availability="failed", claims=[], evidence={},
                             note=f"could not parse BRREG entity JSON: {exc}")

    returned_org = str(payload.get("organisasjonsnummer") or "")
    if returned_org != org:
        # Identity self-check on a K2 source: the API returned a payload
        # whose own key does not match what we asked for. Never publish.
        return ModuleResult(availability="failed", claims=[], evidence={},
                             note=f"BRREG returned organisasjonsnummer={returned_org!r} for requested {org!r}")

    is_deleted = bool(payload.get("slettedato"))

    ev_id = f"{org}:entity"
    ev = _evidence_from_fetch(ev_id, url, result, source_class="official_registry",
                               extractor="brreg_entity@1.0",
                               locator={"json_pointer": "/"})
    claims: list[Claim] = []
    evidence = {ev_id: ev}

    def add(field_name: str, value, availability="available"):
        if value in (None, "", []):
            return
        cid = make_claim_id(org, field_name, value, source_key=f"brreg:entity:{field_name}")
        claims.append(Claim(
            claim_id=cid, organisation_number=org, field=field_name, value=value,
            unit=None, scope=None, reporting_period=None, availability=availability,
            identity_basis="K2:brreg_key", evidence_ids=[ev_id],
        ))

    add("identity.legal_name", payload.get("navn"))
    add("identity.legal_form_code", (payload.get("organisasjonsform") or {}).get("kode"))
    add("identity.legal_form_description", (payload.get("organisasjonsform") or {}).get("beskrivelse"))
    add("identity.registration_date", payload.get("registreringsdatoEnhetsregisteret"))
    add("identity.industry_code", (payload.get("naeringskode1") or {}).get("kode"))
    add("identity.industry_description", (payload.get("naeringskode1") or {}).get("beskrivelse"))
    add("identity.employees_registered", payload.get("antallAnsatte"))
    add("identity.registry_declared_website", payload.get("hjemmeside"))
    add("identity.bankrupt", payload.get("konkurs"))
    add("identity.under_liquidation", payload.get("underAvvikling"))
    add("identity.dissolved_date", payload.get("slettedato"))

    addr = payload.get("forretningsadresse") or {}
    if addr:
        add("workplace.business_address", {
            "street": " ".join(addr.get("adresse") or []) or None,
            "postal_code": addr.get("postnummer"),
            "city": addr.get("poststed"),
            "municipality": addr.get("kommune"),
            "municipality_number": addr.get("kommunenummer"),
        })

    parent = payload.get("overordnetEnhet")
    if parent:
        add("identity.parent_organisation_number", str(parent))

    result_module = ModuleResult(
        availability="not_applicable" if is_deleted else "available",
        claims=[] if is_deleted else claims,
        evidence=evidence,
        raw=payload,
        note="entity is dissolved (slettedato set); no external facts should be asserted for it" if is_deleted else "",
    )
    return result_module


def fetch_accounts(org: str, fetcher: BudgetedFetcher, *, max_periods: int = 6) -> ModuleResult:
    url = ENDPOINTS["accounts"].format(base=BASE, org=org)
    result = _get_registry_with_404_retry(url, fetcher)
    avail = _availability_for_fetch(result)
    if avail != "available":
        return ModuleResult(availability=avail, claims=[], evidence={},
                             note=f"{result.error_kind or result.status}: {result.error_detail}")

    import json
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except Exception as exc:
        return ModuleResult(availability="failed", claims=[], evidence={},
                             note=f"could not parse BRREG accounts JSON: {exc}")

    if not isinstance(payload, list) or not payload:
        return ModuleResult(availability="not_available", claims=[], evidence={},
                             note="no filed accounts records returned")

    claims: list[Claim] = []
    evidence: dict[str, Evidence] = {}

    for idx, record in enumerate(payload[:max_periods]):
        period = record.get("regnskapsperiode") or {}
        period_norm = {
            "from": period.get("fraDato"),
            "to": period.get("tilDato"),
        }
        # SELSKAP vs KONSERN: never collapse this distinction (see
        # CURRENT_SCORING_MODEL adversarial test #30 -- a consolidated
        # figure presented as a standalone company figure is a fabricated-
        # -financial-value-adjacent risk).
        scope = record.get("regnskapstype")  # e.g. "SELSKAP" | "KONSERN"
        currency = record.get("valuta", "NOK")

        ev_id = f"{org}:accounts:{idx}"
        ev = _evidence_from_fetch(ev_id, url, result, source_class="official_filing",
                                   extractor="brreg_accounts@1.0",
                                   locator={"json_pointer": f"/{idx}"})
        evidence[ev_id] = ev

        income = (record.get("resultatregnskapResultat") or {}).get("driftsinntekter") or {}
        revenue = income.get("sumDriftsinntekter")
        result_year = (record.get("resultatregnskapResultat") or {}).get("aarsresultat")

        for field_name, value, unit in (
            ("accounts.revenue", revenue, currency),
            ("accounts.annual_result", result_year, currency),
        ):
            if value is None:
                continue
            cid = make_claim_id(org, field_name, value, scope=scope, period=period_norm,
                                 source_key=f"brreg:accounts:{field_name}:{period_norm.get('to')}")
            claims.append(Claim(
                claim_id=cid, organisation_number=org, field=field_name, value=value,
                unit=unit, scope=scope, reporting_period=period_norm,
                availability="available", identity_basis="K2:brreg_key",
                evidence_ids=[ev_id],
            ))

    if not claims:
        return ModuleResult(availability="not_available", claims=[], evidence=evidence,
                             note="accounts records present but no numeric figures could be extracted "
                                  "(e.g. regulated-industry schema); reported honestly, not as zero")

    return ModuleResult(availability="available", claims=claims, evidence=evidence, raw=payload)


def _as_text(value) -> Optional[str]:
    """Some BRREG role-holder fields (notably a role held by another
    COMPANY rather than a person) return the name as a list of strings
    instead of a plain string. Observed on real, live data on 2026-09-30
    (six real companies in a 10-company test batch). Coerce defensively
    rather than let unicodedata.normalize() crash on a non-string.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [str(v) for v in value if v]
        return " ".join(parts) if parts else None
    return str(value)


def fetch_roles(org: str, fetcher: BudgetedFetcher) -> ModuleResult:
    url = ENDPOINTS["roles"].format(base=BASE, org=org)
    result = _get_registry_with_404_retry(url, fetcher)
    avail = _availability_for_fetch(result)
    if avail != "available":
        return ModuleResult(availability=avail, claims=[], evidence={},
                             note=f"{result.error_kind or result.status}: {result.error_detail}")

    import json
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except Exception as exc:
        return ModuleResult(availability="failed", claims=[], evidence={},
                             note=f"could not parse BRREG roles JSON: {exc}")

    groups = payload.get("rollegrupper") or []
    # Build a canonical, ORDER-INDEPENDENT list of roles. This directly
    # fixes the reproduced refresh bug where re-ordering the same roles was
    # reported as a change (see REFRESH_STRATEGY.md / test #25).
    people = []
    for group in groups:
        group_type = (group.get("type") or {}).get("kode")
        for role in group.get("roller", []):
            role_code = (role.get("type") or {}).get("kode")
            person = role.get("person") or {}
            name = person.get("navn") or {}
            full_name = " ".join(filter(None, [
                _as_text(name.get("fornavn")), _as_text(name.get("mellomnavn")), _as_text(name.get("etternavn")),
            ]))
            entity = role.get("enhet") or {}
            entity_name = _as_text(entity.get("navn"))
            people.append({
                "role_group": group_type,
                "role_code": role_code,
                "person_name": canonicalize_text(full_name) if full_name else None,
                "entity_name": canonicalize_text(entity_name) if entity_name else None,
                "resigned": bool(role.get("fratraadt")),
            })

    people.sort(key=lambda r: (r["role_group"] or "", r["role_code"] or "",
                                r["person_name"] or r["entity_name"] or ""))

    if not people:
        return ModuleResult(availability="not_available", claims=[], evidence={},
                             note="no roles returned (checked; genuinely empty, not zero)")

    ev_id = f"{org}:roles"
    ev = _evidence_from_fetch(ev_id, url, result, source_class="official_registry",
                               extractor="brreg_roles@1.0", locator={"json_pointer": "/"})
    cid = make_claim_id(org, "leadership.roles", people, source_key="brreg:roles")
    claim = Claim(
        claim_id=cid, organisation_number=org, field="leadership.roles", value=people,
        unit=None, scope=None, reporting_period=None, availability="available",
        identity_basis="K2:brreg_key", evidence_ids=[ev_id],
    )
    return ModuleResult(availability="available", claims=[claim], evidence={ev_id: ev}, raw=payload)


def fetch_subunits(org: str, fetcher: BudgetedFetcher) -> ModuleResult:
    url = ENDPOINTS["subunits"].format(base=BASE, org=org)
    result = _get_registry_with_404_retry(url, fetcher)
    avail = _availability_for_fetch(result)
    if avail != "available":
        return ModuleResult(availability=avail, claims=[], evidence={},
                             note=f"{result.error_kind or result.status}: {result.error_detail}")

    import json
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except Exception as exc:
        return ModuleResult(availability="failed", claims=[], evidence={},
                             note=f"could not parse BRREG subunits JSON: {exc}")

    embedded = (payload.get("_embedded") or {}).get("underenheter") or []
    if not embedded:
        return ModuleResult(availability="not_available", claims=[], evidence={},
                             note="no registered subunits (checked; genuinely empty, not zero)")

    subunits = []
    for u in embedded:
        addr = u.get("beliggenhetsadresse") or {}
        subunits.append({
            "organisation_number": str(u.get("organisasjonsnummer")),
            "name": u.get("navn"),
            "municipality": addr.get("kommune"),
            "postal_code": addr.get("postnummer"),
        })
    subunits.sort(key=lambda s: s["organisation_number"])

    ev_id = f"{org}:subunits"
    ev = _evidence_from_fetch(ev_id, url, result, source_class="official_registry",
                               extractor="brreg_subunits@1.0", locator={"json_pointer": "/_embedded/underenheter"})
    cid = make_claim_id(org, "workplace.subunits", subunits, source_key="brreg:subunits")
    claim = Claim(
        claim_id=cid, organisation_number=org, field="workplace.subunits", value=subunits,
        unit=None, scope=None, reporting_period=None, availability="available",
        identity_basis="K2:brreg_key", evidence_ids=[ev_id],
    )
    return ModuleResult(availability="available", claims=[claim], evidence={ev_id: ev}, raw=payload,
                         note="")


def subunit_numbers(subunits_result: ModuleResult) -> set[str]:
    for claim in subunits_result.claims:
        if claim.field == "workplace.subunits":
            return {s["organisation_number"] for s in claim.value}
    return set()
