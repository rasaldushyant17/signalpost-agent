from signalpost_agent.claims import (
    Claim, Evidence, make_claim_id, field_key, verify_span, validate_claim,
    canonicalize_text,
)


def test_claim_id_deterministic_same_fact():
    id1 = make_claim_id("923609016", "accounts.revenue", 1_000_000, scope="SELSKAP",
                         period={"from": "2025-01-01", "to": "2025-12-31"}, source_key="brreg:regnskap")
    id2 = make_claim_id("923609016", "accounts.revenue", 1_000_000, scope="SELSKAP",
                         period={"from": "2025-01-01", "to": "2025-12-31"}, source_key="brreg:regnskap")
    assert id1 == id2


def test_claim_id_changes_with_value():
    id1 = make_claim_id("923609016", "accounts.revenue", 1_000_000, scope="SELSKAP",
                         period={"from": "2025-01-01", "to": "2025-12-31"}, source_key="brreg:regnskap")
    id2 = make_claim_id("923609016", "accounts.revenue", 2_000_000, scope="SELSKAP",
                         period={"from": "2025-01-01", "to": "2025-12-31"}, source_key="brreg:regnskap")
    assert id1 != id2


def test_claim_id_insensitive_to_whitespace_and_unicode_form():
    import unicodedata
    a = "Bjørn Åsen Entreprenør AS"
    b = unicodedata.normalize("NFD", a)  # decomposed form, same text
    id1 = make_claim_id("900000005", "identity.name", a)
    id2 = make_claim_id("900000005", "identity.name", "  " + b + "  ")
    assert id1 == id2


def test_field_key_stable_across_value_change():
    k1 = field_key("923609016", "accounts.revenue", scope="SELSKAP",
                    period={"from": "2025-01-01", "to": "2025-12-31"})
    k2 = field_key("923609016", "accounts.revenue", scope="SELSKAP",
                    period={"from": "2025-01-01", "to": "2025-12-31"})
    assert k1 == k2


def test_verify_span_true_for_real_substring():
    text = "Kontakt oss. Org.nr. 923 609 016. Telefon: 22334455."
    assert verify_span(text, "Org.nr. 923 609 016")


def test_verify_span_false_for_absent_text():
    text = "Kontakt oss. Telefon: 22334455."
    assert not verify_span(text, "Org.nr. 923 609 016")


def test_verify_span_handles_whitespace_variance():
    text = "Org.nr.   923   609   016   "
    assert verify_span(canonicalize_text(text), "Org.nr. 923 609 016")


def test_validate_claim_rejects_available_without_evidence():
    claim = Claim(
        claim_id="abc", organisation_number="923609016", field="identity.name",
        value="Example AS", unit=None, scope=None, reporting_period=None,
        availability="available", identity_basis="K2:brreg_key", evidence_ids=[],
    )
    problems = validate_claim(claim, {})
    assert any("no evidence_ids" in p for p in problems)


def test_validate_claim_rejects_text_evidence_without_span():
    ev = Evidence(
        id="ev1", source_url="https://x.no/", final_url="https://x.no/",
        source_class="company_owned", retrieved_at="2026-09-22T00:00:00Z",
        http_status=200, content_sha256="deadbeef", snapshot_path="snap/ev1.html",
        locator={"selector": "footer"}, claim_span=None, extractor="site@1.0",
    )
    claim = Claim(
        claim_id="abc", organisation_number="923609016", field="identity.brand",
        value="Example", unit=None, scope=None, reporting_period=None,
        availability="available", identity_basis="K1:footer_org_number",
        evidence_ids=["ev1"],
    )
    problems = validate_claim(claim, {"ev1": ev})
    assert any("no claim_span" in p for p in problems)


def test_validate_claim_requires_scope_and_period_for_accounts():
    ev = Evidence(
        id="ev1", source_url="https://data.brreg.no/x", final_url="https://data.brreg.no/x",
        source_class="official_filing", retrieved_at="2026-09-22T00:00:00Z",
        http_status=200, content_sha256="deadbeef", snapshot_path="snap/ev1.json",
        locator={"json_pointer": "/0/revenue"}, claim_span=None, extractor="brreg@1.0",
    )
    claim = Claim(
        claim_id="abc", organisation_number="923609016", field="accounts.revenue",
        value=1000, unit="NOK", scope=None, reporting_period=None,
        availability="available", identity_basis="K2:brreg_key", evidence_ids=["ev1"],
    )
    problems = validate_claim(claim, {"ev1": ev})
    assert any("scope" in p for p in problems)
    assert any("reporting_period" in p for p in problems)


def test_validate_claim_passes_for_well_formed_registry_claim():
    ev = Evidence(
        id="ev1", source_url="https://data.brreg.no/x", final_url="https://data.brreg.no/x",
        source_class="official_filing", retrieved_at="2026-09-22T00:00:00Z",
        http_status=200, content_sha256="deadbeef", snapshot_path="snap/ev1.json",
        locator={"json_pointer": "/0/revenue"}, claim_span=None, extractor="brreg@1.0",
    )
    claim = Claim(
        claim_id="abc", organisation_number="923609016", field="accounts.revenue",
        value=1000, unit="NOK", scope="SELSKAP",
        reporting_period={"from": "2025-01-01", "to": "2025-12-31"},
        availability="available", identity_basis="K2:brreg_key", evidence_ids=["ev1"],
    )
    assert validate_claim(claim, {"ev1": ev}) == []
