from signalpost_agent.claims import Claim
from signalpost_agent.envelope import (
    build_envelope, section_state_from_claims, validate_envelope_shape,
    SECTION_NAMES, section_for_field,
)

ORG = "923609016"


def _claim(field, value, availability="available"):
    return Claim(claim_id=f"c-{field}", organisation_number=ORG, field=field, value=value,
                 unit=None, scope=None, reporting_period=None, availability=availability,
                 identity_basis="K2:brreg_key", evidence_ids=["ev1"] if availability == "available" else [])


def test_all_seven_sections_always_present_even_when_everything_failed():
    envelope = build_envelope(
        organisation_number=ORG, run_id="r1", started_at="t0", completed_at="t1",
        sections={}, claims=[], evidence={}, changes=[], errors=[{"stage": "fetch", "detail": "all sources down"}],
        operations={"requests": 0}, is_baseline_run=True,
    )
    assert set(envelope["sections"].keys()) == set(SECTION_NAMES)
    for name in SECTION_NAMES:
        assert envelope["sections"][name]["state"] in (
            "available", "not_available", "blocked", "not_applicable", "ambiguous", "failed",
        )


def test_missing_data_is_not_available_never_zero_or_absent():
    claims = []  # nothing found at all for this section
    state = section_state_from_claims(claims, checked_sources=["brreg:roles"])
    assert state.state == "not_available"
    # crucially: this is a string state, not a numeric 0 or empty dict
    assert isinstance(state.state, str)


def test_ambiguous_claims_roll_up_to_ambiguous_section():
    claims = [_claim("website.official", None, availability="ambiguous")]
    state = section_state_from_claims(claims, checked_sources=["registry_site"])
    assert state.state == "ambiguous"


def test_available_claims_roll_up_with_claim_ids():
    claims = [_claim("identity.legal_name", "Example AS")]
    state = section_state_from_claims(claims, checked_sources=["brreg:entity"])
    assert state.state == "available"
    assert state.claim_ids == ["c-identity.legal_name"]


def test_validate_envelope_shape_catches_dangling_evidence_reference():
    envelope = {
        "organisation_number": ORG,
        "sections": {name: {"state": "not_available"} for name in SECTION_NAMES},
        "claims": [{"claim_id": "c1", "availability": "available", "evidence_ids": ["ev-missing"]}],
        "evidence": [],
    }
    problems = validate_envelope_shape(envelope)
    assert any("missing evidence" in p for p in problems)


def test_validate_envelope_shape_catches_missing_section():
    envelope = {
        "organisation_number": ORG,
        "sections": {name: {"state": "not_available"} for name in SECTION_NAMES if name != "refresh_metadata"},
        "claims": [], "evidence": [],
    }
    problems = validate_envelope_shape(envelope)
    assert any("refresh_metadata" in p for p in problems)


def test_section_for_field_routes_to_expected_sections():
    assert section_for_field("accounts.revenue") == "accounts_and_history"
    assert section_for_field("leadership.roles") == "leadership_and_workplaces"
    assert section_for_field("hiring.job_posting") == "hiring_and_dated_activity"
    assert section_for_field("website.official") == "website_and_company_profiles"
