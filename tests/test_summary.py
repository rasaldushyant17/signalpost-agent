from signalpost_agent.claims import Claim
from signalpost_agent.summary import build_summary, containment_check

ORG = "923609016"


def _claim(field, value, **kw):
    defaults = dict(claim_id=f"c-{field}-{value}", organisation_number=ORG, field=field,
                     unit=None, scope=None, reporting_period=None, availability="available",
                     identity_basis="K2:brreg_key", evidence_ids=["ev1"])
    defaults.update(kw)
    return Claim(value=value, **defaults)


def test_summary_includes_name_and_marks_unknowns():
    claims = [_claim("identity.legal_name", "Example AS")]
    result = build_summary(ORG, claims, changes=[])
    assert "Example AS" in result["text"]
    assert "filed accounts" in result["unknowns"]
    assert "verified official website" in result["unknowns"]
    # "hiring activity" is deliberately NOT listed as an unknown here: with
    # no verified website at all, there is nowhere hiring could have been
    # checked in the first place -- that absence is already covered by the
    # "verified official website" unknown, so listing it twice would
    # overstate what was actually attempted.
    assert "hiring activity" not in result["unknowns"]


def test_hiring_activity_is_unknown_only_when_a_site_was_actually_checked():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("website.official", "https://example.no"),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "hiring activity" in result["unknowns"]


def test_summary_never_invents_numbers_not_in_claims():
    claims = [_claim("identity.legal_name", "Example AS")]
    result = build_summary(ORG, claims, changes=[])
    problems = containment_check(result["text"], claims)
    assert problems == []


def test_summary_cites_revenue_with_scope_wording():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("accounts.revenue", 1_234_567, scope="KONSERN", unit="NOK",
               reporting_period={"from": "2025-01-01", "to": "2025-12-31"}),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "consolidated (group)" in result["text"]
    assert "1,234,567" in result["text"]
    assert containment_check(result["text"], claims) == []


def test_summary_names_missing_revenue_when_annual_result_is_available():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("accounts.annual_result", 123_456, scope="SELSKAP", unit="NOK",
               reporting_period={"from": "2025-01-01", "to": "2025-12-31"}),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "revenue" in result["unknowns"]
    assert "filed accounts" not in result["unknowns"]


def test_containment_check_flags_fabricated_number():
    claims = [_claim("identity.legal_name", "Example AS")]
    fabricated_text = "Example AS reported revenue of 9999999 last year."
    problems = containment_check(fabricated_text, claims)
    assert problems  # must be flagged: 9999999 is not backed by any claim


def test_summary_reports_real_changes():
    from signalpost_agent.refresh import ChangeRecord, ChangeState
    claims = [_claim("identity.legal_name", "Example AS")]
    changes = [ChangeRecord(organisation_number=ORG, field="accounts.revenue",
                             state=ChangeState.REAL_CHANGE, field_key="k1")]
    result = build_summary(ORG, claims, changes=changes)
    assert "changed since the previous run" in result["text"]


def test_summary_mentions_city_but_never_the_postal_code_digits():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("workplace.business_address", {
            "street": "Storgata 1", "postal_code": "0155", "city": "OSLO",
            "municipality": "Oslo", "municipality_number": "0301",
        }),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "Oslo" in result["text"]
    assert "0155" not in result["text"]
    assert containment_check(result["text"], claims) == []


def test_summary_mentions_subunit_count():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("workplace.subunits", [{"organisation_number": "900000001", "name": "Branch", "municipality": None, "postal_code": None}]),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "1 registered subunit" in result["text"]


def test_summary_mentions_social_profile_platform_label():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("website.official", "https://example.no"),
        _claim("website.social_profile", "https://www.linkedin.com/company/example-as"),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "LinkedIn" in result["text"]
    assert containment_check(result["text"], claims) == []


def test_summary_mentions_dated_activity_count():
    claims = [
        _claim("identity.legal_name", "Example AS"),
        _claim("website.official", "https://example.no"),
        _claim("activity.dated_item", {"title": "News", "date": "2026-03-01", "url": "https://example.no/news/1"}),
    ]
    result = build_summary(ORG, claims, changes=[])
    assert "1 recent dated item" in result["text"]
    assert containment_check(result["text"], claims) == []
