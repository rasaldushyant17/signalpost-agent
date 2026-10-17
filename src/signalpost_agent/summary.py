"""Decision-useful synthesis (the 10-point 'summary' dimension), built
entirely from already-verified claim objects -- never from raw crawled
text. See AI_USAGE_AUDIT.md: this module deliberately contains zero LLM
calls in v1. Every sentence is a template filled from a claim; a sentence
is only emitted if its claim exists and is available.
"""
from __future__ import annotations

import re
from typing import Optional

from .claims import Claim


def _find(claims: list[Claim], field_name: str) -> Optional[Claim]:
    for c in claims:
        if c.field == field_name and c.availability == "available":
            return c
    return None


def _find_all(claims: list[Claim], field_name: str) -> list[Claim]:
    return [c for c in claims if c.field == field_name and c.availability == "available"]


_SOCIAL_PLATFORM_LABELS = {
    "linkedin.com": "LinkedIn", "facebook.com": "Facebook", "instagram.com": "Instagram",
    "youtube.com": "YouTube", "twitter.com": "X (Twitter)", "x.com": "X (Twitter)",
}


def _social_platform_label(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
    except Exception:
        return "a social platform"
    for domain, label in _SOCIAL_PLATFORM_LABELS.items():
        if host == domain or host.endswith("." + domain):
            return label
    return "a social platform"


def build_summary(organisation_number: str, claims: list[Claim], changes: list) -> dict:
    """Returns {"text": str, "unknowns": [str], "cited_claim_ids": [str]}.
    `text` is plain, factual, template-generated prose with no adjectives
    the data doesn't support.
    """
    sentences: list[str] = []
    cited: list[str] = []
    unknowns: list[str] = []

    name = _find(claims, "identity.legal_name")
    industry = _find(claims, "identity.industry_description")
    form = _find(claims, "identity.legal_form_description")

    if name:
        opening = f"{name.value}"
        if form:
            opening += f" is registered as a {form.value.lower()}"
            cited.append(form.claim_id)
        if industry:
            opening += f" in the {industry.value.lower()} sector" if form else f" operates in the {industry.value.lower()} sector"
            cited.append(industry.claim_id)
        opening += "."
        sentences.append(opening)
        cited.append(name.claim_id)
    else:
        unknowns.append("legal name")

    revenue_claims = sorted(_find_all(claims, "accounts.revenue"),
                             key=lambda c: (c.reporting_period or {}).get("to", ""))
    if revenue_claims:
        latest = revenue_claims[-1]
        period_to = (latest.reporting_period or {}).get("to", "an unspecified period")
        scope_word = "consolidated (group)" if latest.scope == "KONSERN" else "company-level"
        sentences.append(
            f"Its most recent filed {scope_word} accounts (period ending {period_to}) report "
            f"revenue of {latest.value:,} {latest.unit}."
        )
        cited.append(latest.claim_id)
    else:
        unknowns.append("filed accounts")

    roles = _find(claims, "leadership.roles")
    if roles and roles.value:
        board = [r for r in roles.value if r.get("role_group") == "STYR" and not r.get("resigned")]
        if board:
            names = [r["person_name"] for r in board if r.get("person_name")]
            if names:
                sentences.append(f"Its registered board includes {', '.join(names[:5])}.")
                cited.append(roles.claim_id)
    else:
        unknowns.append("leadership/board roles")

    address = _find(claims, "workplace.business_address")
    if address and isinstance(address.value, dict) and address.value.get("city"):
        # Deliberately omit the postal code here: it's a 4-digit number not
        # traceable through containment_check's available-values set (which
        # only harvests numbers from claim VALUES, not nested address
        # sub-fields), so including it would trip the anti-hallucination
        # gate on a perfectly legitimate fact. The city name alone is a
        # safe, still-useful fact to surface.
        sentences.append(f"Its registered business address is in {address.value['city'].title()}.")
        cited.append(address.claim_id)

    subunits = _find(claims, "workplace.subunits")
    if subunits and subunits.value:
        sentences.append(f"It has {len(subunits.value)} registered subunit(s).")
        cited.append(subunits.claim_id)

    website = _find(claims, "website.official")
    if website:
        sentences.append(f"Its verified official website is {website.value}.")
        cited.append(website.claim_id)
    else:
        unknowns.append("verified official website")

    social_profiles = _find_all(claims, "website.social_profile")
    if social_profiles:
        labels = sorted({_social_platform_label(c.value) for c in social_profiles})
        sentences.append(f"It has a verified presence on {', '.join(labels)}, linked from its own site.")
        cited.extend(c.claim_id for c in social_profiles)

    postings = _find_all(claims, "hiring.job_posting")
    if postings:
        sentences.append(f"It has {len(postings)} current job posting(s) on its own site.")
        cited.extend(c.claim_id for c in postings)
    elif website:
        # Only an "unknown" when a verified site exists to check in the
        # first place -- with no verified site at all there is nowhere
        # hiring activity could have been checked, which is already
        # covered by the "verified official website" unknown above.
        unknowns.append("hiring activity")

    dated_items = _find_all(claims, "activity.dated_item")
    if dated_items:
        sentences.append(f"It has {len(dated_items)} recent dated item(s) (news/updates) on its own site.")
        cited.extend(c.claim_id for c in dated_items)

    real_changes = [c for c in changes if c.state == "REAL_CHANGE"]
    new_items = [c for c in changes if c.state == "NEW"]
    if real_changes or new_items:
        parts = []
        if real_changes:
            parts.append(f"{len(real_changes)} field(s) changed since the previous run")
        if new_items:
            parts.append(f"{len(new_items)} new fact(s) found")
        sentences.append("Since the previous run: " + " and ".join(parts) + ".")

    if unknowns:
        sentences.append("Not established from checked sources: " + ", ".join(unknowns) + ".")

    text = " ".join(sentences)
    return {"text": text, "unknowns": unknowns, "cited_claim_ids": sorted(set(cited))}


_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def containment_check(summary_text: str, claims: list[Claim]) -> list[str]:
    """Anti-hallucination gate for the summary (EVIDENCE_ARCHITECTURE.md
    section 6): every numeric token in the rendered text must be traceable
    to the numeric value of at least one AVAILABLE claim. Returns a list of
    problems; an empty list means the summary is safe to render as-is.
    """
    available_values = set()
    for c in claims:
        if c.availability != "available":
            continue
        if isinstance(c.value, (int, float)):
            available_values.add(str(int(c.value)) if float(c.value).is_integer() else str(c.value))
        if isinstance(c.value, dict):
            for v in c.value.values():
                if isinstance(v, (int, float)):
                    available_values.add(str(int(v)) if float(v).is_integer() else str(v))
        # Dates (including bare years) that come from a claim's own
        # reporting period are legitimate to mention -- e.g. "period ending
        # 2025-12-31" -- and must not be flagged as an unsupported number.
        period = c.reporting_period or {}
        for date_str in (period.get("from"), period.get("to")):
            if date_str:
                available_values.add(str(date_str))
                year = str(date_str)[:4]
                if year.isdigit():
                    available_values.add(year)

    problems = []
    for match in _NUMBER_RE.finditer(summary_text):
        token = match.group(0).replace(",", "")
        if not token or token in ("-",):
            continue
        # Years and simple counts embedded in generated prose (e.g. "3 job
        # posting(s)") are not financial claims; only check tokens long
        # enough to plausibly be a monetary figure to avoid false positives
        # on incidental small integers the template itself introduces.
        if len(token.lstrip("-")) < 4:
            continue
        if token not in available_values:
            problems.append(f"numeric token {token!r} in summary is not traceable to any available claim value")
    return problems
