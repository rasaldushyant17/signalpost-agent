import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from build_viewer import build_viewer_html, _slim  # noqa: E402

ORG = "923609016"


def _envelope(**overrides):
    base = {
        "organisation_number": ORG,
        "run": {"run_id": "abc123", "started_at": "t1", "completed_at": "t2",
                "terminal_status": "completed", "is_baseline_run": True},
        "sections": {
            "legal_identity_and_brand": {
                "state": "available", "claim_ids": ["c1"], "checked_sources": ["brreg:entity"], "note": "",
            },
            "website_and_company_profiles": {
                "state": "available", "claim_ids": ["c2"], "checked_sources": ["https://example.no"], "note": "",
            },
        },
        "claims": [
            {"claim_id": "c1", "organisation_number": ORG, "field": "identity.legal_name",
             "value": "Example AS", "unit": None, "scope": None, "reporting_period": None,
             "availability": "available", "identity_basis": "K2:brreg_key", "evidence_ids": ["e1"]},
            {"claim_id": "c2", "organisation_number": ORG, "field": "website.official",
             "value": "https://example.no", "unit": None, "scope": None, "reporting_period": None,
             "availability": "available", "identity_basis": "K1:contextual_org_number", "evidence_ids": ["e2"]},
        ],
        "evidence": [
            {"id": "e1", "source_url": "https://data.brreg.no/x", "final_url": "https://data.brreg.no/x",
             "source_class": "official_registry", "retrieved_at": "t", "http_status": 200,
             "content_sha256": "abc", "snapshot_path": None, "locator": {}, "claim_span": None,
             "extractor": "x", "redirect_chain": []},
            {"id": "e2", "source_url": "https://example.no", "final_url": "https://example.no",
             "source_class": "company_owned", "retrieved_at": "t", "http_status": 200,
             "content_sha256": "def", "snapshot_path": None, "locator": {}, "claim_span": "923 609 016",
             "extractor": "x", "redirect_chain": []},
        ],
        "changes": [],
        "errors": [],
        "operations": {},
        "summary": {"text": "Example AS is registered as a company.", "unknowns": [], "cited_claim_ids": []},
    }
    base.update(overrides)
    return base


def test_slim_extracts_legal_name_and_evidence_urls():
    row = _slim(_envelope())
    assert row["org"] == ORG
    assert row["name"] == "Example AS"
    website_claims = row["sections"]["website_and_company_profiles"]["claims"]
    assert website_claims[0]["evidence_urls"] == ["https://example.no"]


def test_viewer_html_is_self_contained_and_includes_company_data():
    page = build_viewer_html([_envelope()])
    assert "<!doctype html>" in page.lower()
    assert "<script src=" not in page  # no external script dependency
    assert "http://" not in page.split("<script")[0].replace("https://", "")  # no http(s) stylesheet/link refs before scripts
    assert ORG in page
    assert "Example AS" in page
    assert "https://example.no" in page  # evidence link embedded
    assert 'name="viewport"' in page  # mobile-safe viewport meta


def test_viewer_handles_empty_batch_without_crashing():
    page = build_viewer_html([])
    assert "<!doctype html>" in page.lower()


def test_viewer_escapes_script_closing_tag_in_embedded_data():
    # A claim value containing the literal substring "</script" (e.g. from
    # scraped page text) must never be able to break out of the embedded
    # JSON <script> tag.
    envelope = _envelope()
    envelope["claims"][0]["value"] = "Evil Co </script><script>alert(1)</script>"
    page = build_viewer_html([envelope])
    assert "<script>alert(1)</script>" not in page
