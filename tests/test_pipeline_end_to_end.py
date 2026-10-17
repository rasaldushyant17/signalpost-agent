import json
import tempfile

from signalpost_agent import registry_brreg as reg
from signalpost_agent.pipeline import run_company
from signalpost_agent.refresh import HistoryStore
from signalpost_agent.envelope import validate_envelope_shape, SECTION_NAMES
from fake_fetcher import FakeFetcher, Canned

ORG = "923609016"


def _urls(org=ORG):
    return {
        "entity": reg.ENDPOINTS["entity"].format(base=reg.BASE, org=org),
        "accounts": reg.ENDPOINTS["accounts"].format(base=reg.BASE, org=org),
        "roles": reg.ENDPOINTS["roles"].format(base=reg.BASE, org=org),
        "subunits": reg.ENDPOINTS["subunits"].format(base=reg.BASE, org=org),
    }


def _entity_payload(org=ORG, **overrides):
    payload = {
        "organisasjonsnummer": org, "navn": "Example AS",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmering"},
        "hjemmeside": "example.no",
        "forretningsadresse": {"adresse": ["Storgata 1"], "postnummer": "0155", "poststed": "OSLO"},
    }
    payload.update(overrides)
    return payload


def _full_fetcher(website_body: bytes | None = None, website_status: int = 200,
                   org=ORG, entity_overrides=None):
    urls = _urls(org)
    responses = {
        urls["entity"]: Canned(body=json.dumps(_entity_payload(org, **(entity_overrides or {}))).encode()),
        urls["accounts"]: Canned(body=json.dumps([{
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "regnskapstype": "SELSKAP", "valuta": "NOK",
            "resultatregnskapResultat": {"driftsinntekter": {"sumDriftsinntekter": 5_000_000}},
        }]).encode()),
        urls["roles"]: Canned(body=json.dumps({"rollegrupper": [{
            "type": {"kode": "STYR"},
            "roller": [{"type": {"kode": "LEDE"}, "person": {"navn": {"fornavn": "Anna", "etternavn": "Hansen"}}}],
        }]}).encode()),
        urls["subunits"]: Canned(body=json.dumps({"_embedded": {"underenheter": []}}).encode()),
    }
    if website_body is not None:
        responses["https://example.no"] = Canned(status=website_status, ok=website_status == 200,
                                                    body=website_body)
    return FakeFetcher(responses)


def test_happy_path_produces_valid_complete_envelope():
    site_html = (
        b'<html><head><title>Example AS</title></head><body>'
        b'Kontakt oss. Org.nr. 923 609 016. Vi leverer tjenester over hele landet i mange aar.'
        b'</body></html>'
    )
    fetcher = _full_fetcher(website_body=site_html)
    envelope = run_company(ORG, fetcher=fetcher, history=None)

    problems = validate_envelope_shape(envelope)
    assert problems == [], problems
    assert envelope["sections"]["legal_identity_and_brand"]["state"] == "available"
    assert envelope["sections"]["accounts_and_history"]["state"] == "available"
    assert envelope["sections"]["website_and_company_profiles"]["state"] == "available"
    assert envelope["errors"] == []


def test_every_source_down_still_produces_exactly_one_valid_envelope():
    # No responses registered at all -> everything 404s. This must NOT
    # crash, and every section must still be present with an honest state.
    fetcher = FakeFetcher({})
    envelope = run_company(ORG, fetcher=fetcher, history=None)
    assert set(envelope["sections"].keys()) == set(SECTION_NAMES)
    for name in SECTION_NAMES:
        assert envelope["sections"][name]["state"] in (
            "available", "not_available", "blocked", "not_applicable", "ambiguous", "failed",
        )
    assert validate_envelope_shape(envelope) == []


def test_invalid_organisation_number_produces_isolated_failure_envelope():
    envelope = run_company("123456789", fetcher=FakeFetcher({}), history=None)
    assert envelope["errors"]
    assert envelope["errors"][0]["error_type"] == "InvalidOrganisationNumber"
    assert envelope["organisation_number"] == "123456789"


def test_a_raising_module_does_not_crash_the_pipeline():
    # Simulate a fetcher whose .get() raises for one endpoint -- proves the
    # _safe() isolation boundary actually isolates, not just typed fetch
    # errors but genuine unexpected exceptions from any stage.
    urls = _urls()

    class ExplodingFetcher:
        def get(self, url, accept=None):
            if url == urls["accounts"]:
                raise RuntimeError("simulated unexpected crash deep in a parser")
            return FakeFetcher({
                urls["entity"]: Canned(body=json.dumps(_entity_payload()).encode()),
                urls["roles"]: Canned(body=json.dumps({"rollegrupper": []}).encode()),
                urls["subunits"]: Canned(body=json.dumps({"_embedded": {"underenheter": []}}).encode()),
            }).get(url, accept=accept)

    envelope = run_company(ORG, fetcher=ExplodingFetcher(), history=None)
    assert envelope is not None
    assert any(e["error_type"] == "RuntimeError" for e in envelope["errors"])
    # The rest of the profile still completed despite the accounts crash.
    assert envelope["sections"]["legal_identity_and_brand"]["state"] == "available"
    assert validate_envelope_shape(envelope) == []


def test_budget_exhaustion_shows_as_blocked_not_a_silent_not_available():
    # Reproduces a real diagnostic gap found while debugging a live
    # 1,000-company run: when a module's fetch failed for any reason other
    # than a genuine "not found" (e.g. the request budget ran out), the
    # ModuleResult's real availability ("blocked") was being discarded
    # because it carried no claims, and the section silently defaulted to
    # "not_available" regardless of the true cause -- making it impossible
    # to tell "genuinely absent" apart from "we gave up for budget reasons"
    # just by looking at the envelope.
    from signalpost_agent.budget import BudgetGovernor
    gov = BudgetGovernor(max_requests=0, max_wall_seconds=999)  # exhausted before the first request
    fetcher = _full_fetcher()  # would otherwise succeed fully
    fetcher.governor = gov  # FakeFetcher doesn't consult a governor itself, so simulate
    # the exhaustion directly via a thin wrapper that mimics BudgetedFetcher's
    # own behaviour: refuse every call once the governor says no.
    class GovernedFakeFetcher:
        def __init__(self, inner, governor):
            self.inner = inner
            self.governor = governor

        def get(self, url, accept=None):
            if not self.governor.allow_request():
                from signalpost_agent.http_client import FetchResult, ErrorKind
                return FetchResult(url=url, final_url=url, ok=False,
                                    error_kind=ErrorKind.BLOCKED_POLICY,
                                    error_detail="budget exhausted (simulated)")
            return self.inner.get(url, accept=accept)

    envelope = run_company(ORG, fetcher=GovernedFakeFetcher(fetcher, gov), history=None)
    assert envelope["sections"]["legal_identity_and_brand"]["state"] == "blocked"


def test_dissolved_entity_asserts_no_external_facts():
    fetcher = _full_fetcher(entity_overrides={"slettedato": "2026-01-01"})
    envelope = run_company(ORG, fetcher=fetcher, history=None)
    assert envelope["sections"]["accounts_and_history"]["state"] == "not_available"
    assert envelope["sections"]["website_and_company_profiles"]["state"] == "not_available"


def test_group_listing_website_is_ambiguous_not_published():
    site_html = (
        b'<html><head><title>Konsern</title></head><body>'
        b'Vaare selskaper: Datterselskap Ett AS (org.nr. 900000014), '
        b'Example AS (org.nr. 923609016), Datterselskap To AS (org.nr. 900000022).'
        b'</body></html>'
    )
    fetcher = _full_fetcher(website_body=site_html)
    envelope = run_company(ORG, fetcher=fetcher, history=None)
    assert envelope["sections"]["website_and_company_profiles"]["state"] == "ambiguous"


def test_refresh_across_two_runs_with_history_store():
    with tempfile.TemporaryDirectory() as tmp:
        history = HistoryStore(tmp)
        fetcher1 = _full_fetcher()
        env1 = run_company(ORG, fetcher=fetcher1, history=history)
        assert env1["run"]["is_baseline_run"] is True

        fetcher2 = _full_fetcher()  # identical data
        env2 = run_company(ORG, fetcher=fetcher2, history=history)
        assert env2["run"]["is_baseline_run"] is False
        change_states = {c["state"] for c in env2["changes"]}
        assert "REAL_CHANGE" not in change_states  # nothing actually changed
        assert "NEW" not in change_states  # not a fresh discovery either


def test_refresh_detects_real_revenue_change():
    with tempfile.TemporaryDirectory() as tmp:
        history = HistoryStore(tmp)
        run_company(ORG, fetcher=_full_fetcher(), history=history)

        urls = _urls()
        fetcher2 = _full_fetcher()
        fetcher2.responses[urls["accounts"]] = Canned(body=json.dumps([{
            "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
            "regnskapstype": "SELSKAP", "valuta": "NOK",
            "resultatregnskapResultat": {"driftsinntekter": {"sumDriftsinntekter": 7_000_000}},
        }]).encode())
        env2 = run_company(ORG, fetcher=fetcher2, history=history)
        real_changes = [c for c in env2["changes"] if c["state"] == "REAL_CHANGE"]
        assert any(c["field"] == "accounts.revenue" for c in real_changes)


def test_no_registry_website_falls_back_to_discovered_domain_and_publishes():
    # The explicit gap Builderr's own scoring feedback named twice: a
    # company with NO registry-declared website must still get a
    # discovered-and-verified one when the guessed domain actually belongs
    # to it (organisation number present in context).
    org = "923609016"
    urls = _urls(org)
    responses = {
        urls["entity"]: Canned(body=json.dumps(_entity_payload(org, **{"hjemmeside": None})).encode()),
        urls["accounts"]: Canned(body=json.dumps([]).encode()),
        urls["roles"]: Canned(body=json.dumps({"rollegrupper": []}).encode()),
        urls["subunits"]: Canned(body=json.dumps({"_embedded": {"underenheter": []}}).encode()),
        # "Example AS" -> discovery.candidate_domains() tries example.no first.
        "https://example.no": Canned(body=(
            b'<html><head><title>Example AS</title></head><body>'
            b'Kontakt oss. Org.nr. 923 609 016. Vi leverer tjenester over hele landet.'
            b'</body></html>'
        )),
    }
    envelope = run_company(org, fetcher=FakeFetcher(responses), history=None)
    assert envelope["sections"]["website_and_company_profiles"]["state"] == "available"
    website_claims = [c for c in envelope["claims"] if c["field"] == "website.official"]
    assert len(website_claims) == 1
    assert website_claims[0]["value"] == "https://example.no"
    assert validate_envelope_shape(envelope) == []
    sources = envelope["sections"]["website_and_company_profiles"]["checked_sources"]
    assert any("discovery_candidate" in s for s in sources)


def test_wrong_domain_guess_is_never_published_without_org_number_match():
    # The discovery candidate resolves and returns 200, but the page has no
    # trace of the target's organisation number -- this must NOT be
    # published just because the domain guess "sounds right" (the exact
    # discipline IDENTITY_SAFETY.md requires of every source, guessed or
    # declared).
    org = "923609016"
    urls = _urls(org)
    responses = {
        urls["entity"]: Canned(body=json.dumps(_entity_payload(org, **{"hjemmeside": None})).encode()),
        urls["accounts"]: Canned(body=json.dumps([]).encode()),
        urls["roles"]: Canned(body=json.dumps({"rollegrupper": []}).encode()),
        urls["subunits"]: Canned(body=json.dumps({"_embedded": {"underenheter": []}}).encode()),
        "https://example.no": Canned(body=(
            b'<html><head><title>Unrelated Business</title></head><body>'
            b'This site belongs to someone else entirely and never mentions '
            b'the target company or its organisation number anywhere.'
            b'</body></html>'
        )),
    }
    envelope = run_company(org, fetcher=FakeFetcher(responses), history=None)
    website_claims = [c for c in envelope["claims"] if c["field"] == "website.official"]
    assert website_claims == []
    assert envelope["sections"]["website_and_company_profiles"]["state"] == "not_available"


def test_registry_site_fails_identity_check_then_discovery_still_tries():
    # Registry declares a site, but it turns out to be a directory/parked
    # page (so the registry-path publish fails); discovery must still get
    # a chance afterwards rather than giving up on the company entirely.
    org = "923609016"
    urls = _urls(org)
    responses = {
        urls["entity"]: Canned(body=json.dumps(_entity_payload(org, **{"hjemmeside": "badsite.no"})).encode()),
        urls["accounts"]: Canned(body=json.dumps([]).encode()),
        urls["roles"]: Canned(body=json.dumps({"rollegrupper": []}).encode()),
        urls["subunits"]: Canned(body=json.dumps({"_embedded": {"underenheter": []}}).encode()),
        "https://badsite.no": Canned(body=b"<html><body>domain is for sale</body></html>"),
        "https://example.no": Canned(body=(
            b'<html><head><title>Example AS</title></head><body>'
            b'Org.nr. 923 609 016. Riktig selskap.'
            b'</body></html>'
        )),
    }
    envelope = run_company(org, fetcher=FakeFetcher(responses), history=None)
    assert envelope["sections"]["website_and_company_profiles"]["state"] == "available"
    website_claims = [c for c in envelope["claims"] if c["field"] == "website.official"]
    assert website_claims[0]["value"] == "https://example.no"


def test_social_link_on_verified_site_is_published_as_company_profile():
    org = "923609016"
    urls = _urls(org)
    site_html = (
        b'<html><head><title>Example AS</title></head><body>'
        b'Org.nr. 923 609 016. Folg oss: '
        b'<a href="https://www.linkedin.com/company/example-as">LinkedIn</a>'
        b'</body></html>'
    )
    fetcher = _full_fetcher(org=org, website_body=site_html)
    envelope = run_company(org, fetcher=fetcher, history=None)
    social_claims = [c for c in envelope["claims"] if c["field"] == "website.social_profile"]
    assert len(social_claims) == 1
    assert social_claims[0]["value"] == "https://www.linkedin.com/company/example-as"
    assert social_claims[0]["identity_basis"] == "L:linked_from_verified_site"
    assert validate_envelope_shape(envelope) == []


def test_source_failure_on_rerun_never_reported_as_change():
    with tempfile.TemporaryDirectory() as tmp:
        history = HistoryStore(tmp)
        run_company(ORG, fetcher=_full_fetcher(), history=history)

        urls = _urls()
        fetcher2 = _full_fetcher()
        fetcher2.responses[urls["accounts"]] = Canned(ok=False, status=503)
        env2 = run_company(ORG, fetcher=fetcher2, history=history)
        states = {c["field"]: c["state"] for c in env2["changes"]}
        assert states.get("accounts.revenue") == "SOURCE_UNAVAILABLE"
