"""Norwegian organisation-number validation and normalisation.

The organisation number is the single stable identity key for the whole
pipeline (see IDENTITY_SAFETY.md). Every place that accepts a number from
user input, a crawled page, or a JSON payload must go through here first.
"""
from __future__ import annotations

import re

_DIGITS_ONLY = re.compile(r"\D+")
_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)


def normalize(raw: str) -> str:
    """Strip everything but digits. Does NOT validate length or checksum."""
    return _DIGITS_ONLY.sub("", raw or "")


def is_valid(org_number: str) -> bool:
    """True iff org_number is exactly 9 digits and passes the mod-11 check
    digit used by the Norwegian Central Coordinating Register (Brreg).
    """
    if not re.fullmatch(r"\d{9}", org_number or ""):
        return False
    total = sum(int(d) * w for d, w in zip(org_number[:8], _WEIGHTS))
    remainder = total % 11
    check = 0 if remainder == 0 else 11 - remainder
    if check == 10:
        return False  # 10 is not a valid check digit; number is invalid
    return check == int(org_number[8])


def parse_or_none(raw: str) -> str | None:
    """Normalise and validate in one step. Returns None (never raises) if
    the input cannot be a real Norwegian organisation number.
    """
    candidate = normalize(raw)
    return candidate if is_valid(candidate) else None


def find_all_contextual(text: str, *, window: int = 40) -> list[dict]:
    """Find checksum-valid organisation numbers that appear near a labelling
    keyword (org.nr, organisasjonsnummer, NO ... MVA, etc.) in free text.

    This is deliberately NOT a bare digit-substring search across the whole
    page (that is exactly the bug reproduced against the reference starter
    kit in IDENTITY_SAFETY.md, section 5). A number only counts as a
    "contextual key" (tier K1) if:
      1. it is checksum-valid, and
      2. a recognised label appears within `window` characters before it,
         OR it is written as 'NO 123456789MVA' / 'NO 123 456 789 MVA'.

    Returns a list of {"number": str, "span": str, "start": int, "end": int}
    dicts, one per contextual hit (duplicates are not de-duplicated here;
    callers decide how to use repeated hits).
    """
    if not text:
        return []

    hits: list[dict] = []

    # Pattern 1: label ... number (with optional spaces/dots inside the number)
    label = r"(org\.?\s*(?:nr|nummer)|organisasjonsnummer|orgnr|enhetsregisteret)"
    number = r"(\d[\d\s.]{7,13}\d)"
    for m in re.finditer(label + r"\s*[:.]?\s*" + number, text, flags=re.IGNORECASE):
        digits = _DIGITS_ONLY.sub("", m.group(2))
        if len(digits) == 9 and is_valid(digits):
            hits.append({
                "number": digits,
                "span": m.group(0).strip(),
                "start": m.start(),
                "end": m.end(),
            })

    # Pattern 2: 'NO 923 609 016 MVA' VAT-style formatting
    for m in re.finditer(r"\bNO[\s]?(\d[\d\s]{7,13}\d)[\s]?MVA\b", text, flags=re.IGNORECASE):
        digits = _DIGITS_ONLY.sub("", m.group(1))
        if len(digits) == 9 and is_valid(digits):
            hits.append({
                "number": digits,
                "span": m.group(0).strip(),
                "start": m.start(),
                "end": m.end(),
            })

    return hits
