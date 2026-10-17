import json

from signalpost_agent import registry_brreg as reg
from fake_fetcher import FakeFetcher, Canned

ORG = "923609016"
ENTITY_URL = reg.ENDPOINTS["entity"].format(base=reg.BASE, org=ORG)
ACCOUNTS_URL = reg.ENDPOINTS["accounts"].format(base=reg.BASE, org=ORG)
ROLES_URL = reg.ENDPOINTS["roles"].format(base=reg.BASE, org=ORG)
SUBUNITS_URL = reg.ENDPOINTS["subunits"].format(base=reg.BASE, org=ORG)


def entity_payload(**overrides):
    payload = {
        "organisasjonsnummer": ORG,
        "navn": "EXAMPLE AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "registreringsdatoEnhetsregisteret": "1995-01-01",
        "naeringskode1": {"kode": "68.209", "beskrivelse": "Utleie"},
        "antallAnsatte": 12,
        "hjemmeside": "example.no",
        "forretningsadresse": {"adresse": ["Storgata 1"], "postnummer": "0155",
                                "poststed": "OSLO", "kommune": "OSLO", "kommunenummer": "0301"},
        "konkurs": False,
        "underAvvikling": False,
    }
    payload.update(overrides)
    return payload


def test_fetch_entity_happy_path():
    fetcher = FakeFetcher({ENTITY_URL: Canned(body=json.dumps(entity_payload()).encode())})
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "available"
    fields = {c.field: c.value for c in result.claims}
    assert fields["identity.legal_name"] == "EXAMPLE AS"
    assert fields["identity.legal_form_code"] == "AS"
    assert fields["identity.registry_declared_website"] == "example.no"


def test_fetch_entity_rejects_mismatched_organisation_number():
    # If BRREG ever returned a payload keyed to a DIFFERENT org number than
    # requested, this must fail loudly, not silently publish under the
    # wrong company.
    bad_payload = entity_payload(organisasjonsnummer="999999999")
    fetcher = FakeFetcher({ENTITY_URL: Canned(body=json.dumps(bad_payload).encode())})
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "failed"
    assert result.claims == []


def test_fetch_entity_deleted_entity_asserts_nothing():
    payload = entity_payload(slettedato="2026-01-01")
    fetcher = FakeFetcher({ENTITY_URL: Canned(body=json.dumps(payload).encode())})
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "not_applicable"
    assert result.claims == []  # no external facts asserted for a dissolved entity


def test_fetch_entity_404_is_not_available_not_zero():
    fetcher = FakeFetcher({ENTITY_URL: Canned(ok=False, status=404)})
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "not_available"
    assert result.claims == []


def test_fetch_entity_retries_a_transient_registry_404():
    # Reproduces a real bug found on a live 1,000-company run against the
    # actual Norwegian registry on 2026-09-30: ~64% of entity lookups came
    # back with a plain 404 for organisation numbers that, re-checked
    # moments later, resolved correctly every time. A genuine, permanent
    # 404 (the entity really doesn't exist) must still end up
    # not_available; a transient one must be rescued by the retry.
    # (the real backoff sleep is neutralized suite-wide by conftest.py --
    # this test only needs to prove the retry COUNT and outcome)
    fetcher = FakeFetcher()
    fetcher.add_sequence(ENTITY_URL, [
        Canned(ok=False, status=404),
        Canned(ok=False, status=404),
        Canned(body=json.dumps(entity_payload()).encode()),  # succeeds on the 3rd try
    ])
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "available"
    assert fetcher.calls.count(ENTITY_URL) == 3


def test_fetch_entity_genuine_404_still_ends_as_not_available():
    # A 404 that never resolves (because the entity really isn't there)
    # must still end up honestly not_available after the retries are
    # exhausted -- the retry is a safety net, not a way to hide real gaps.
    fetcher = FakeFetcher({ENTITY_URL: Canned(ok=False, status=404)})
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "not_available"
    assert fetcher.calls.count(ENTITY_URL) == 4  # 1 initial + 3 retries, all exhausted


def test_fetch_entity_malformed_json_is_failed_not_crash():
    fetcher = FakeFetcher({ENTITY_URL: Canned(body=b"{not json")})
    result = reg.fetch_entity(ORG, fetcher)
    assert result.availability == "failed"


def test_fetch_accounts_tags_scope_and_period():
    payload = [{
        "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
        "regnskapstype": "SELSKAP",
        "valuta": "NOK",
        "resultatregnskapResultat": {
            "driftsinntekter": {"sumDriftsinntekter": 1_000_000},
            "aarsresultat": 100_000,
        },
    }]
    fetcher = FakeFetcher({ACCOUNTS_URL: Canned(body=json.dumps(payload).encode())})
    result = reg.fetch_accounts(ORG, fetcher)
    assert result.availability == "available"
    rev = next(c for c in result.claims if c.field == "accounts.revenue")
    assert rev.scope == "SELSKAP"
    assert rev.reporting_period == {"from": "2025-01-01", "to": "2025-12-31"}
    assert rev.unit == "NOK"
    assert rev.value == 1_000_000


def test_fetch_accounts_konsern_scope_is_preserved_not_dropped():
    # Adversarial test #30: a consolidated (KONSERN) figure must never be
    # presented unlabelled as the entity's own standalone figure.
    payload = [{
        "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
        "regnskapstype": "KONSERN",
        "valuta": "NOK",
        "resultatregnskapResultat": {"driftsinntekter": {"sumDriftsinntekter": 9_000_000}},
    }]
    fetcher = FakeFetcher({ACCOUNTS_URL: Canned(body=json.dumps(payload).encode())})
    result = reg.fetch_accounts(ORG, fetcher)
    rev = next(c for c in result.claims if c.field == "accounts.revenue")
    assert rev.scope == "KONSERN"


def test_fetch_accounts_none_filed_is_not_available_not_zero():
    fetcher = FakeFetcher({ACCOUNTS_URL: Canned(body=json.dumps([]).encode())})
    result = reg.fetch_accounts(ORG, fetcher)
    assert result.availability == "not_available"
    assert result.claims == []


def test_fetch_accounts_service_error_is_failed_and_isolated():
    fetcher = FakeFetcher({ACCOUNTS_URL: Canned(ok=False, status=503)})
    result = reg.fetch_accounts(ORG, fetcher)
    assert result.availability == "failed"


def _roles_payload(order):
    groups = [{
        "type": {"kode": "STYR", "beskrivelse": "Styre"},
        "roller": [
            {"type": {"kode": code}, "person": {"navn": {"fornavn": first, "etternavn": "X"}}, "fratraadt": False}
            for (first, code) in order
        ],
    }]
    return {"rollegrupper": groups}


def test_fetch_roles_is_order_insensitive():
    fetcher_a = FakeFetcher({ROLES_URL: Canned(body=json.dumps(
        _roles_payload([("Anna", "LEDE"), ("Bjorn", "MEDL")])).encode())})
    fetcher_b = FakeFetcher({ROLES_URL: Canned(body=json.dumps(
        _roles_payload([("Bjorn", "MEDL"), ("Anna", "LEDE")])).encode())})
    result_a = reg.fetch_roles(ORG, fetcher_a)
    result_b = reg.fetch_roles(ORG, fetcher_b)
    claim_a = next(c for c in result_a.claims if c.field == "leadership.roles")
    claim_b = next(c for c in result_b.claims if c.field == "leadership.roles")
    # Same claim_id regardless of source ordering -- this is what fixes the
    # reproduced refresh false-change bug (order re-shuffled -> "change").
    assert claim_a.claim_id == claim_b.claim_id


def test_fetch_roles_handles_company_role_holder_with_list_shaped_name():
    # Reproduces a real bug found on live BRREG data on 2026-09-30: when a
    # role is held by another COMPANY rather than a person, the holder's
    # "navn" field can come back as a list of strings instead of a plain
    # string, which crashed unicodedata.normalize() before this fix.
    payload = {"rollegrupper": [{
        "type": {"kode": "STYR", "beskrivelse": "Styre"},
        "roller": [{
            "type": {"kode": "MEDL"},
            "enhet": {"organisasjonsnummer": "915637353", "navn": ["Holding", "Company AS"]},
        }],
    }]}
    fetcher = FakeFetcher({ROLES_URL: Canned(body=json.dumps(payload).encode())})
    result = reg.fetch_roles(ORG, fetcher)
    assert result.availability == "available"
    roles_claim = next(c for c in result.claims if c.field == "leadership.roles")
    assert roles_claim.value[0]["entity_name"] == "Holding Company AS"


def test_fetch_roles_empty_is_not_available_not_zero():
    fetcher = FakeFetcher({ROLES_URL: Canned(body=json.dumps({"rollegrupper": []}).encode())})
    result = reg.fetch_roles(ORG, fetcher)
    assert result.availability == "not_available"


def test_fetch_subunits_sorted_and_typed():
    payload = {"_embedded": {"underenheter": [
        {"organisasjonsnummer": "923609024", "navn": "EXAMPLE AS AVD OSLO",
         "beliggenhetsadresse": {"kommune": "OSLO", "postnummer": "0155"}},
    ]}}
    fetcher = FakeFetcher({SUBUNITS_URL: Canned(body=json.dumps(payload).encode())})
    result = reg.fetch_subunits(ORG, fetcher)
    assert result.availability == "available"
    numbers = reg.subunit_numbers(result)
    assert numbers == {"923609024"}


def test_fetch_subunits_none_registered_is_not_available_not_zero():
    fetcher = FakeFetcher({SUBUNITS_URL: Canned(body=json.dumps({"_embedded": {"underenheter": []}}).encode())})
    result = reg.fetch_subunits(ORG, fetcher)
    assert result.availability == "not_available"
