"""Identity safety: deciding whether a crawled page really belongs to the
target company.

This module directly replaces the reference starter kit's
`assess_website_identity`, which was shown (IDENTITY_SAFETY.md) to return
`exact, 0.95` for:
  * a subsidiary whose legal-name tokens are a subset of its parent
    group's homepage title, and
  * a single-token legal name that merely appears in an unrelated site's
    title,
and `exact, 1.0` for every subsidiary listed (with its own org number) on
a shared group homepage.

The rule here is deliberately conservative: PUBLISH only on a strong,
contextual proof (tier K1/K2). Everything else is RETAIN (a candidate,
not a claim) or REJECT. See IDENTITY_SAFETY.md sections 3-4 for the full
design rationale; this module is the executable form of that document.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from . import org_number as onr

LEGAL_FORM_SUFFIXES = (
    "AS", "ASA", "ANS", "DA", "BA", "SA", "KS", "NUF", "ENK", "BRL", "ESEK",
    "STI", "FLI",
    # Public-enterprise forms (BRREG organisasjonsform codes), added because
    # Builderr's own scoring feedback named a real example of this exact
    # gap ("SUNNAAS SYKEHUS HF"): HF = helseforetak (health trust),
    # IKS = interkommunalt selskap, KF/FKF = kommunalt/fylkeskommunalt
    # foretak, SF = statsforetak. Without stripping these, domain-candidate
    # generation (discovery.py) would guess "sunnaassykehushf.no" instead
    # of the real "sunnaas.no" pattern.
    "HF", "IKS", "KF", "FKF", "SF",
)

_PARKED_MARKERS = (
    "domain is for sale", "domeneparkering", "this domain is parked",
    "buy this domain", "under construction", "coming soon",
    "default web page", "apache2 ubuntu default page", "index of /",
)

_DIRECTORY_HOST_MARKERS = (
    "proff.no", "1881.no", "gulesider.no", "regnskapstall.no",
    "linkedin.com", "facebook.com", "instagram.com", "purehelp.no",
)


@dataclass
class IdentityDecision:
    decision: str                 # "publish" | "retain" | "reject" | "ambiguous"
    tier: Optional[str]           # "K1" | "K2" | "N" | "A" | "L" | "W" | None
    identity_basis: Optional[str]  # human-readable, e.g. "K1:footer_org_number"
    reasons: list[str] = field(default_factory=list)
    matched_span: Optional[str] = None


def normalize_legal_name(name: str, *, strip_legal_form: bool = False) -> str:
    text = unicodedata.normalize("NFC", name or "").upper()
    text = re.sub(r"[.,]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if strip_legal_form:
        tokens = text.split(" ")
        while tokens and tokens[-1] in LEGAL_FORM_SUFFIXES:
            tokens.pop()
        text = " ".join(tokens)
    return text


def _looks_parked(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PARKED_MARKERS)


def _is_directory_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _DIRECTORY_HOST_MARKERS)


def assess_website_identity(
    *,
    target_org_number: str,
    target_legal_name: str,
    candidate_host: str,
    homepage_text: str,
    homepage_title: str = "",
    known_subunit_numbers: Optional[set[str]] = None,
    is_directory_or_social_host: Optional[bool] = None,
) -> IdentityDecision:
    """Decide whether `candidate_host`'s homepage may be published as the
    target's official website.

    All inputs are assumed already correctly decoded (see website.py for
    charset handling -- decoding failures must never reach this function
    disguised as valid text).
    """
    reasons: list[str] = []
    known_subunit_numbers = known_subunit_numbers or set()

    if is_directory_or_social_host is None:
        is_directory_or_social_host = _is_directory_host(candidate_host)
    if is_directory_or_social_host:
        return IdentityDecision("reject", None, None,
                                 ["host is a directory/social/aggregator platform, not a company site"])

    if _looks_parked(homepage_text) or _looks_parked(homepage_title):
        return IdentityDecision("reject", None, None, ["homepage shows parked/placeholder markers"])

    full_text = f"{homepage_title}\n{homepage_text}"
    hits = onr.find_all_contextual(full_text)
    valid_hits = [h for h in hits if onr.is_valid(h["number"])]

    distinct_numbers = {h["number"] for h in valid_hits}
    target_hits = [h for h in valid_hits if h["number"] == target_org_number]

    # X3: a group/listing page naming multiple distinct organisation
    # numbers in context. This is the exact "shared homepage lists every
    # subsidiary" trap. Even if the target's own number IS present, we
    # cannot tell from digits alone whether the target is the page's
    # operator or merely one of several listed entities -> ambiguous.
    if len(distinct_numbers) >= 2:
        if target_org_number in distinct_numbers:
            reasons.append(
                f"page labels {len(distinct_numbers)} distinct organisation numbers "
                "including the target's own -- cannot tell whether target is the "
                "operator or merely listed (group/franchise page)"
            )
            return IdentityDecision("ambiguous", None, None, reasons,
                                     matched_span=target_hits[0]["span"])
        else:
            reasons.append("page labels other organisations' numbers, not the target's")
            return IdentityDecision("reject", None, None, reasons)

    # K1: exactly the target's own checksum-valid number, in a labelled
    # context, and no other entity's number competing on the same page.
    if target_hits:
        hit = target_hits[0]
        return IdentityDecision(
            "publish", "K1", "K1:contextual_org_number",
            [f"target organisation number found in labelled context: {hit['span']!r}"],
            matched_span=hit["span"],
        )

    # A labelled number belonging to a KNOWN subunit of the target
    # corroborates (tier A-ish) but does not by itself prove the top-level
    # entity's own site -- retain as a candidate rather than reject or
    # publish outright.
    if distinct_numbers and distinct_numbers & known_subunit_numbers:
        reasons.append("page labels a known subunit's organisation number, not the parent's own")
        return IdentityDecision("retain", "A", None, reasons)

    # No K1. Full legal name (including legal form) as a supporting-only
    # signal -- tier N. Deliberately NOT sufficient alone in v1 (see
    # IDENTITY_SAFETY.md section 4.1): retained, not published, until a
    # blind audit justifies promoting it.
    target_full = normalize_legal_name(target_legal_name, strip_legal_form=False)
    haystack = normalize_legal_name(full_text, strip_legal_form=False)
    if target_full and target_full in haystack:
        reasons.append("full legal name (including legal form) found on the page, but no organisation-number proof")
        return IdentityDecision("retain", "N", None, reasons)

    reasons.append("no organisation-number or full-legal-name evidence found")
    return IdentityDecision("reject", None, None, reasons)


def check_redirect_domain(requested_registered_domain: str, final_registered_domain: str) -> IdentityDecision | None:
    """If a fetch redirected to a DIFFERENT registered domain, the final
    domain must be treated as a brand-new, unverified candidate -- it does
    NOT inherit trust from the originally requested (e.g. registry-declared)
    URL. Returns an IdentityDecision('reject', ...) hint when domains
    differ, or None when the caller should proceed to assess the final
    page normally (domains match, or the comparison could not be made).
    """
    if not requested_registered_domain or not final_registered_domain:
        return None
    if requested_registered_domain.lower() != final_registered_domain.lower():
        return IdentityDecision(
            "reject", None, None,
            [
                f"redirected from {requested_registered_domain!r} to a different "
                f"registered domain {final_registered_domain!r}; final domain "
                "requires its own independent identity proof before anything is trusted"
            ],
        )
    return None


def assess_social_link(
    *,
    link_url: str,
    was_linked_from_verified_site: bool,
) -> IdentityDecision:
    """Company-owned social/video profiles are only ever published as a
    reciprocal link (tier L) FROM an already-verified site. They are never
    independently crawled/verified on the platform itself.
    """
    if was_linked_from_verified_site:
        return IdentityDecision("publish", "L", "L:linked_from_verified_site",
                                 ["profile URL found as an outbound link on the verified official site"])
    return IdentityDecision("retain", None, None,
                             ["profile candidate not linked from any verified site"])
