from signalpost_agent import website as ws
from signalpost_agent.http_client import FetchResult


def test_registered_domain_extraction():
    assert ws.registered_domain("https://www.bergen-bygg.no/about") == "bergen-bygg.no"
    assert ws.registered_domain("example.no") == "example.no"


def test_decode_page_declared_utf8():
    body = "Bjørn Åsen Entreprenør AS".encode("utf-8")
    text, note = ws.decode_page({"Content-Type": "text/html; charset=utf-8"}, body)
    assert text == "Bjørn Åsen Entreprenør AS"


def test_decode_page_declared_latin1_is_decoded_correctly():
    # This is the exact bug reproduced in STARTER_FORENSIC_AUDIT.md: the
    # reference kit always decoded as utf-8 with errors='replace', mangling
    # a real Latin-1 site's æøå and defeating identity matching. Here the
    # charset is honoured, so the real name comes through intact.
    original = "Bjørn Åsen Entreprenør AS - Byggmester"
    body = original.encode("latin-1")
    text, note = ws.decode_page({"Content-Type": "text/html; charset=iso-8859-1"}, body)
    assert text == original


def test_decode_page_undeclared_latin1_fails_closed_not_mojibake():
    # No charset declared anywhere, and the bytes are NOT valid UTF-8.
    # Must return None (fail closed), never a garbled best-effort string
    # that could defeat a later name/org-number match silently.
    original = "Bjørn Åsen Entreprenør AS"
    body = original.encode("latin-1")
    text, note = ws.decode_page({}, body)
    assert text is None


def test_decode_page_no_charset_but_valid_utf8_still_works():
    body = "Kaffebrenneriet".encode("utf-8")
    text, note = ws.decode_page({}, body)
    assert text == "Kaffebrenneriet"


def test_classify_links_buckets_careers_news_about_social():
    links = [
        "https://example.no/karriere/utvikler",
        "https://example.no/nyheter/2026-lansering",
        "https://example.no/om-oss",
        "https://www.linkedin.com/company/example",
        "https://someunrelatedsite.no/careers",  # different domain, must be excluded
    ]
    buckets = ws.classify_links(links, "example.no")
    assert buckets["careers"] == ["https://example.no/karriere/utvikler"]
    assert buckets["news"] == ["https://example.no/nyheter/2026-lansering"]
    assert buckets["about"] == ["https://example.no/om-oss"]
    assert buckets["social"] == ["https://www.linkedin.com/company/example"]


def test_extract_job_postings_structured_only():
    capture = ws.PageCapture(
        url="https://example.no/karriere", final_url="https://example.no/karriere",
        ok=True, status=200, text="<html></html>", decode_note="",
        json_ld=[{"@type": "JobPosting", "title": "Utvikler", "datePosted": "2026-09-01",
                  "url": "https://example.no/karriere/utvikler"}],
    )
    postings = ws.extract_job_postings(capture)
    assert len(postings) == 1
    assert postings[0]["title"] == "Utvikler"


def test_extract_job_postings_ignores_unstructured_text():
    # No JSON-LD present -> must return nothing, never guess from prose.
    capture = ws.PageCapture(
        url="https://example.no/karriere", final_url="https://example.no/karriere",
        ok=True, status=200,
        text="<html><body>Vi søker en utvikler! Send CV til oss.</body></html>",
        decode_note="",
    )
    assert ws.extract_job_postings(capture) == []


def test_extract_dated_articles_from_time_tag_fallback():
    capture = ws.PageCapture(
        url="https://example.no/nyheter", final_url="https://example.no/nyheter",
        ok=True, status=200,
        text='<html><body><article><time datetime="2026-06-01">Ny kontrakt signert</time></article></body></html>',
        decode_note="",
    )
    items = ws.extract_dated_articles(capture)
    assert len(items) == 1
    assert items[0]["date"] == "2026-06-01"


def test_fetch_and_parse_flags_cross_domain_redirect():
    class StubFetcher:
        def get(self, url, accept=None):
            return FetchResult(url=url, final_url="https://totallydifferent.com/",
                                status=200, ok=True, body=b"<html><title>Hi</title></html>",
                                headers={"Content-Type": "text/html; charset=utf-8"},
                                content_sha256="abc", redirect_chain=["https://example.no/"])
    capture = ws.fetch_and_parse("https://example.no/", StubFetcher(),
                                  requested_registered_domain="example.no")
    assert capture.redirect_domain_changed is True


def test_fetch_and_parse_same_domain_redirect_not_flagged():
    class StubFetcher:
        def get(self, url, accept=None):
            return FetchResult(url=url, final_url="https://www.example.no/",
                                status=200, ok=True, body=b"<html><title>Hi</title></html>",
                                headers={"Content-Type": "text/html; charset=utf-8"},
                                content_sha256="abc", redirect_chain=["https://example.no/"])
    capture = ws.fetch_and_parse("https://example.no/", StubFetcher(),
                                  requested_registered_domain="example.no")
    assert capture.redirect_domain_changed is False


def test_fetch_and_parse_propagates_fetch_failure_without_crashing():
    class FailingFetcher:
        def get(self, url, accept=None):
            return FetchResult(url=url, final_url=url, ok=False, status=None,
                                error_kind="truncated_body", error_detail="boom")
    capture = ws.fetch_and_parse("https://example.no/", FailingFetcher())
    assert capture.ok is False
    assert capture.fetch_error_kind == "truncated_body"
