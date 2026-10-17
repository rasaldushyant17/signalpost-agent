"""One company, start to finish: BRREG -> identity-gated website (declared
site, then independent domain-guess discovery as a fallback) -> claims
-> refresh diff -> summary -> terminal envelope.

The single most important property of this module (RESOURCE_BUDGET.md,
ADVERSARIAL_TEST_PLAN #24): whatever goes wrong inside `run_company`, it
ALWAYS returns an envelope. Nothing here may raise past this module's
public entry point -- batch.py depends on that guarantee to produce
exactly N envelopes no matter what happens to any single company.
"""
from __future__ import annotations

import hashlib
import time
import traceback
from typing import Optional

from . import registry_brreg as brreg
from . import website as ws
from . import identity as idn
from . import org_number as onr
from . import discovery as disc
from .claims import Claim, Evidence, make_claim_id, validate_claim, verify_span
from .envelope import build_envelope, section_state_from_claims, SectionState
from .http_client import BudgetedFetcher
from .refresh import diff_company, HistoryStore
from .summary import build_summary, containment_check


def now_iso() -> str:
    return brreg.now_iso()


def _safe(stage_name: str, errors: list, fn, *args, **kwargs):
    """Runs fn(*args, **kwargs); on ANY exception, records it in `errors`
    and returns None instead of propagating. This is the boundary that
    guarantees run_company can never crash the caller.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # last-resort isolation, deliberately broad
        errors.append({
            "stage": stage_name,
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "traceback": traceback.format_exc(limit=6),
        })
        return None


def _website_claim(org: str, url: str, decision: idn.IdentityDecision, capture, extra_evidence: dict) -> tuple[Optional[Claim], dict]:
    """Builds a website.official claim from a published identity decision,
    or returns (None, {}) for anything that isn't a clean publish.
    """
    if decision.decision != "publish":
        return None, {}
    # NOTE: previously used Python's built-in hash(url), which is salted
    # per-process by design (hash randomization, for security) -- meaning
    # the SAME url produced a DIFFERENT id in every separate run of the
    # script. Confirmed as a real reproducibility bug on a live run. A
    # SHA-256 digest of the url is stable across processes and runs.
    import hashlib as _hashlib
    ev_id = f"{org}:website:{_hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"
    ev = Evidence(
        id=ev_id, source_url=url, final_url=capture.final_url,
        source_class="company_owned", retrieved_at=now_iso(),
        http_status=capture.status, content_sha256=capture.content_sha256,
        snapshot_path=None,
        locator={"selector": "homepage"}, claim_span=decision.matched_span,
        extractor="website_identity@1.0", redirect_chain=capture.redirect_chain,
    )
    cid_source = f"website:{url}"
    from .claims import make_claim_id
    cid = make_claim_id(org, "website.official", capture.final_url, source_key=cid_source)
    claim = Claim(
        claim_id=cid, organisation_number=org, field="website.official", value=capture.final_url,
        unit=None, scope=None, reporting_period=None, availability="available",
        identity_basis=decision.identity_basis, evidence_ids=[ev_id],
    )
    return claim, {ev_id: ev}


def _process_candidate_website(
    *,
    org: str,
    url: str,
    fetcher: BudgetedFetcher,
    errors: list,
    target_name: Optional[str],
    known_subunits: set,
    all_claims: list,
    all_evidence: dict,
    section_claims: dict,
    checked_sources: dict,
    source_label: str,
) -> bool:
    """Attempts to verify and publish ONE candidate URL as the company's
    official website, and -- only if that publish succeeds -- extracts
    careers/news/social-profile claims from it. Shared by both the
    registry-declared-site path and the independent-discovery path below,
    so a guessed domain is put through exactly the same identity-safety
    gate as a registry-declared one (never published on name match alone).

    Returns True iff a website.official claim was published (callers use
    this to decide whether to keep trying further candidates).
    """
    checked_sources.setdefault("website_and_company_profiles", []).append(source_label)

    requested_domain = _safe("website.domain_parse", errors, ws.registered_domain, url) or ""
    capture = _safe("website.fetch", errors, ws.fetch_and_parse, url, fetcher,
                     requested_registered_domain=requested_domain)
    if capture is None:
        return False

    if capture.redirect_domain_changed:
        ambiguous_claim = Claim(
            claim_id=f"website-ambiguous-{org}-{requested_domain}", organisation_number=org,
            field="website.official", value=None, unit=None, scope=None,
            reporting_period=None, availability="ambiguous",
            identity_basis=None, evidence_ids=[],
        )
        all_claims.append(ambiguous_claim)
        section_claims.setdefault("website_and_company_profiles", []).append(ambiguous_claim)
        return False

    if not capture.ok:
        return False

    text = ws.visible_text(capture)
    decision = idn.assess_website_identity(
        target_org_number=org,
        target_legal_name=target_name or "",
        candidate_host=requested_domain,
        homepage_text=text,
        homepage_title=capture.title,
        known_subunit_numbers=known_subunits,
    )

    if decision.decision == "ambiguous":
        ambiguous_claim = Claim(
            claim_id=f"website-ambiguous-{org}-{requested_domain}", organisation_number=org,
            field="website.official", value=None, unit=None, scope=None,
            reporting_period=None, availability="ambiguous",
            identity_basis=None, evidence_ids=[],
        )
        all_claims.append(ambiguous_claim)
        section_claims.setdefault("website_and_company_profiles", []).append(ambiguous_claim)
        return False

    if decision.decision != "publish":
        return False  # reject / retain: not a clean enough match to publish

    claim, ev = _website_claim(org, url, decision, capture, {})
    if claim is None:
        return False
    all_claims.append(claim)
    all_evidence.update(ev)
    section_claims.setdefault("website_and_company_profiles", []).append(claim)

    buckets = ws.classify_links(capture.links, requested_domain)

    # Social / profile links -- tier L: only ever published as a reciprocal
    # link FOUND ON this now-verified official site, never by independently
    # crawling or trusting the platform itself (identity.assess_social_link).
    # This is the explicit "social profile" fact named in Builderr's own
    # scoring feedback.
    for social_url in buckets["social"]:
        social_decision = idn.assess_social_link(link_url=social_url, was_linked_from_verified_site=True)
        if social_decision.decision != "publish":
            continue
        if not verify_span(capture.text or "", social_url):
            # Fail closed: the link must literally appear in the bytes we
            # actually fetched (same anti-hallucination discipline as
            # every other claim in this agent), not merely in the parsed
            # link list.
            continue
        sid = make_claim_id(org, "website.social_profile", social_url, source_key=f"homepage:{url}")
        ev_id = f"{org}:social:{sid[:8]}"
        all_evidence[ev_id] = Evidence(
            id=ev_id, source_url=url, final_url=capture.final_url,
            source_class="company_owned", retrieved_at=now_iso(),
            http_status=capture.status, content_sha256=capture.content_sha256,
            snapshot_path=None, locator={"selector": "homepage_outbound_link"},
            claim_span=social_url, extractor="social_link@1.0",
        )
        sclaim = Claim(
            claim_id=sid, organisation_number=org, field="website.social_profile",
            value=social_url, unit=None, scope=None, reporting_period=None,
            availability="available", identity_basis=social_decision.identity_basis,
            evidence_ids=[ev_id],
        )
        all_claims.append(sclaim)
        section_claims.setdefault("website_and_company_profiles", []).append(sclaim)

    for careers_url in buckets["careers"][:1]:
        cc = _safe("website.careers", errors, ws.fetch_and_parse, careers_url, fetcher,
                   requested_registered_domain=requested_domain)
        if cc and cc.ok:
            for posting in ws.extract_job_postings(cc):
                if not posting.get("title"):
                    continue
                pid = make_claim_id(org, "hiring.job_posting", posting,
                                     source_key=f"careers:{careers_url}")
                ev_id = f"{org}:jobposting:{pid[:8]}"
                all_evidence[ev_id] = Evidence(
                    id=ev_id, source_url=careers_url, final_url=cc.final_url,
                    source_class="company_owned", retrieved_at=now_iso(),
                    http_status=cc.status, content_sha256=cc.content_sha256,
                    snapshot_path=None, locator={"json_ld": "JobPosting"},
                    claim_span=posting.get("title"), extractor="jsonld_jobposting@1.0",
                )
                pclaim = Claim(
                    claim_id=pid, organisation_number=org, field="hiring.job_posting",
                    value=posting, unit=None, scope=None, reporting_period=None,
                    availability="available", identity_basis=decision.identity_basis,
                    evidence_ids=[ev_id],
                )
                all_claims.append(pclaim)
                section_claims.setdefault("hiring_and_dated_activity", []).append(pclaim)

    for news_url in buckets["news"][:1]:
        nc = _safe("website.news", errors, ws.fetch_and_parse, news_url, fetcher,
                   requested_registered_domain=requested_domain)
        if nc and nc.ok:
            for item in ws.extract_dated_articles(nc):
                if not item.get("date"):
                    continue
                aid = make_claim_id(org, "activity.dated_item", item,
                                     source_key=f"news:{news_url}")
                ev_id = f"{org}:activity:{aid[:8]}"
                all_evidence[ev_id] = Evidence(
                    id=ev_id, source_url=news_url, final_url=nc.final_url,
                    source_class="company_owned", retrieved_at=now_iso(),
                    http_status=nc.status, content_sha256=nc.content_sha256,
                    snapshot_path=None, locator={"selector": "time"},
                    claim_span=item.get("date"), extractor="dated_item@1.0",
                )
                aclaim = Claim(
                    claim_id=aid, organisation_number=org, field="activity.dated_item",
                    value=item, unit=None, scope=None, reporting_period=None,
                    availability="available", identity_basis=decision.identity_basis,
                    evidence_ids=[ev_id],
                )
                all_claims.append(aclaim)
                section_claims.setdefault("hiring_and_dated_activity", []).append(aclaim)

    return True


def run_company(
    org: str,
    *,
    fetcher: BudgetedFetcher,
    history: Optional[HistoryStore],
    fetch_website: bool = True,
) -> dict:
    """Returns a complete terminal envelope dict for `org`. Never raises."""
    started_at = now_iso()
    # Deterministic, not a random UUID: the same organisation number
    # always gets the same run_id, so this field never differs between
    # two separate invocations and never needs special-casing when
    # comparing runs for stability (unlike started_at/completed_at below,
    # which legitimately do vary with real wall-clock time, the same way
    # an evidence record's own retrieved_at correctly does).
    run_id = hashlib.sha256(f"run-id:{org}".encode("utf-8")).hexdigest()[:12]
    errors: list[dict] = []
    all_claims: list[Claim] = []
    all_evidence: dict[str, Evidence] = {}
    section_claims: dict[str, list[Claim]] = {}
    checked_sources: dict[str, list[str]] = {}

    def record(section: str, module_result, source_label: str):
        checked_sources.setdefault(section, []).append(source_label)
        section_claims.setdefault(section, [])
        if module_result is None:
            return
        if module_result.availability != "available" and not module_result.claims:
            # Without this, the TRUE reason (blocked / failed / not_applicable
            # / not_available) was silently lost: section_state_from_claims
            # only reads claim.availability values, and an empty claims list
            # always fell through to its generic "not_available" default
            # regardless of what actually happened. That masked a real
            # request-budget exhaustion as an indistinguishable "not found"
            # on a live 1,000-company run, which made the true cause much
            # harder to diagnose than it needed to be. A sentinel claim
            # carries the real reason through to the envelope.
            sentinel = Claim(
                claim_id=f"{org}:{section}:module-status:{module_result.availability}",
                organisation_number=org, field=f"{section}.module_status", value=None,
                unit=None, scope=None, reporting_period=None,
                availability=module_result.availability, identity_basis=None, evidence_ids=[],
            )
            all_claims.append(sentinel)
            section_claims[section].append(sentinel)
        for c in module_result.claims:
            all_claims.append(c)
            section_claims[section].append(c)
        all_evidence.update(module_result.evidence)

    org_normalised = onr.normalize(org)
    if not onr.is_valid(org_normalised):
        return _terminal_failure_envelope(org, run_id, started_at,
                                           reason=f"invalid organisation number (failed mod-11 checksum): {org!r}")

    org = org_normalised

    entity_result = _safe("registry.entity", errors, brreg.fetch_entity, org, fetcher)
    record("legal_identity_and_brand", entity_result, "brreg:entity")

    is_dissolved = entity_result is not None and entity_result.availability == "not_applicable"

    accounts_result = None
    roles_result = None
    subunits_result = None
    if not is_dissolved:
        accounts_result = _safe("registry.accounts", errors, brreg.fetch_accounts, org, fetcher)
        record("accounts_and_history", accounts_result, "brreg:accounts")

        roles_result = _safe("registry.roles", errors, brreg.fetch_roles, org, fetcher)
        record("leadership_and_workplaces", roles_result, "brreg:roles")

        subunits_result = _safe("registry.subunits", errors, brreg.fetch_subunits, org, fetcher)
        record("leadership_and_workplaces", subunits_result, "brreg:subunits")

    known_subunits = brreg.subunit_numbers(subunits_result) if subunits_result else set()

    registry_site = None
    if entity_result:
        for c in entity_result.claims:
            if c.field == "identity.registry_declared_website" and c.value:
                registry_site = c.value

    website_published = False
    if fetch_website and not is_dissolved:
        target_name = None
        if entity_result:
            for c in entity_result.claims:
                if c.field == "identity.legal_name":
                    target_name = c.value

        tried_domains: set[str] = set()

        if registry_site:
            url = registry_site if registry_site.startswith("http") else f"https://{registry_site}"
            registry_domain = _safe("website.domain_parse", errors, ws.registered_domain, url) or ""
            if registry_domain:
                tried_domains.add(registry_domain)
            website_published = _process_candidate_website(
                org=org, url=url, fetcher=fetcher, errors=errors,
                target_name=target_name, known_subunits=known_subunits,
                all_claims=all_claims, all_evidence=all_evidence,
                section_claims=section_claims, checked_sources=checked_sources,
                source_label=url,
            )

        # Independent website discovery: the registry has no declared
        # website for ~89% of companies (LIMITATIONS.md), and even when it
        # does, that single declared site may be unreachable or fail the
        # identity gate. Builderr's own scoring feedback named this gap
        # explicitly, twice: "No verified company-website profile fact
        # matched in this check. Confirm the legal company/domain, then
        # capture profile facts with direct source URLs." Candidates are
        # deterministic domain guesses from the company's OWN registered
        # legal name (discovery.py) -- no search engine, no paid API, per
        # this agent's declared source policy -- and each one still has to
        # pass the SAME contextual organisation-number identity gate as a
        # registry-declared site (tier K1) before anything is published.
        if not website_published and target_name:
            for domain in disc.candidate_domains(target_name):
                if domain in tried_domains:
                    continue
                tried_domains.add(domain)
                website_published = _process_candidate_website(
                    org=org, url=f"https://{domain}", fetcher=fetcher, errors=errors,
                    target_name=target_name, known_subunits=known_subunits,
                    all_claims=all_claims, all_evidence=all_evidence,
                    section_claims=section_claims, checked_sources=checked_sources,
                    source_label=f"discovery_candidate:https://{domain}",
                )
                if website_published:
                    break

    # Validate every "available" claim before it is allowed into the
    # envelope. A claim that fails validation is dropped (never "repaired")
    # and logged as an error, per EVIDENCE_ARCHITECTURE.md section 7.
    valid_claims = []
    for c in all_claims:
        problems = validate_claim(c, all_evidence) if c.availability == "available" else []
        if problems:
            errors.append({"stage": "claim_validation", "claim_id": c.claim_id,
                           "error_type": "ClaimValidationFailed", "detail": "; ".join(problems)})
            continue
        valid_claims.append(c)

    changes = []
    is_baseline = True
    if history is not None:
        previous = _safe("refresh.load_history", errors, history.load_latest_available_claims, org) or []
        is_baseline = len(previous) == 0
        changes = _safe("refresh.diff", errors, diff_company, org,
                         previous_claims=previous, current_claims=valid_claims, now_iso=now_iso()) or []
        _safe("refresh.append", errors, history.append_run, org, run_id, changes,
              {c.claim_id: c for c in valid_claims}, now_iso())

    summary = build_summary(org, valid_claims, changes)
    contain_problems = containment_check(summary["text"], valid_claims)
    if contain_problems:
        errors.append({"stage": "summary_containment", "error_type": "UnsupportedSummaryContent",
                       "detail": "; ".join(contain_problems)})
        summary["text"] = ("Summary withheld: could not verify every stated figure against "
                           "available evidence. See claims and evidence for the raw facts.")

    sections = {}
    for section in ("legal_identity_and_brand", "accounts_and_history",
                     "leadership_and_workplaces", "website_and_company_profiles",
                     "hiring_and_dated_activity"):
        claims_here = [c for c in valid_claims if c in section_claims.get(section, [])]
        sections[section] = section_state_from_claims(
            claims_here, checked_sources=checked_sources.get(section, []),
        )
    real_claims = [c for c in valid_claims if c.availability == "available"]
    sections["claim_level_evidence"] = SectionState(
        state="available" if real_claims else "not_available",
        claim_ids=[c.claim_id for c in real_claims],
    )
    sections["refresh_metadata"] = SectionState(
        state="not_applicable" if is_baseline else "available",
        note="baseline run (no prior snapshot)" if is_baseline else f"{len(changes)} field(s) diffed",
    )

    completed_at = now_iso()
    envelope = build_envelope(
        organisation_number=org, run_id=run_id, started_at=started_at, completed_at=completed_at,
        sections=sections, claims=valid_claims, evidence=all_evidence, changes=changes,
        errors=errors, operations={"errors_count": len(errors)}, is_baseline_run=is_baseline,
    )
    envelope["summary"] = summary
    return envelope


def _terminal_failure_envelope(org: str, run_id: str, started_at: str, *, reason: str) -> dict:
    completed_at = now_iso()
    return build_envelope(
        organisation_number=org, run_id=run_id, started_at=started_at, completed_at=completed_at,
        sections={}, claims=[], evidence={}, changes=[],
        errors=[{"stage": "input_validation", "error_type": "InvalidOrganisationNumber", "detail": reason}],
        operations={}, is_baseline_run=True,
    )
