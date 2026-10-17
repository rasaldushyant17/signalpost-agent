"""Company website fetch + parse.

Fixes, relative to the reference starter kit (see IDENTITY_SAFETY.md and
STARTER_FORENSIC_AUDIT.md), applied here:
  * charset is detected (HTTP header, then <meta charset>, then a
    conservative fallback), never blindly decoded as UTF-8 with
    errors='replace' -- that mangling was shown to quarantine a real match
    (æ/ø/å corruption defeating name comparison).
  * a homepage redirect to a DIFFERENT registered domain is flagged before
    any identity check runs on the final page (identity.check_redirect_domain).
  * robots.txt is fetched once per host and cached for the whole run
    instead of once per page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import tldextract
from bs4 import BeautifulSoup

from .http_client import BudgetedFetcher, FetchResult

# tldextract ships a bundled public-suffix-list snapshot and falls back to it
# without any network call (verified empirically in development).
_TLD = tldextract.TLDExtract(suffix_list_urls=())

CAREER_PATH_HINTS = ("/karriere", "/careers", "/jobb", "/jobs", "/ledige-stillinger", "/stillinger")
NEWS_PATH_HINTS = ("/news", "/nyheter", "/aktuelt", "/presse", "/blogg", "/blog")
ABOUT_PATH_HINTS = ("/om-oss", "/about", "/kontakt", "/contact")

_CHARSET_META_RE = re.compile(rb'charset=["\']?([a-zA-Z0-9_\-]+)', re.IGNORECASE)


def registered_domain(url_or_host: str) -> str:
    ext = _TLD(url_or_host)
    return ext.top_domain_under_public_suffix.lower()


def _detect_charset(headers: dict, body: bytes) -> str:
    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    m = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip('"').lower()
    m2 = _CHARSET_META_RE.search(body[:4096])
    if m2:
        return m2.group(1).decode("ascii", errors="ignore").lower()
    return ""  # unknown; caller decides fallback policy


_KNOWN_ENCODINGS = {"utf-8", "utf8", "iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252"}


def decode_page(headers: dict, body: bytes) -> tuple[Optional[str], str]:
    """Returns (decoded_text_or_None, note). Never guesses silently: if the
    declared/sniffed charset can't decode the bytes cleanly, or none is
    declared and UTF-8 strict decoding fails, this returns (None, reason)
    rather than a mojibake string (adversarial test #32).
    """
    charset = _detect_charset(headers, body)
    if charset:
        norm = charset.replace("_", "-")
        try:
            return body.decode(norm, errors="strict"), f"decoded as declared charset {charset!r}"
        except (LookupError, UnicodeDecodeError):
            pass  # fall through to strict UTF-8 attempt
    try:
        return body.decode("utf-8", errors="strict"), "decoded as utf-8 (no reliable charset declared)"
    except UnicodeDecodeError as exc:
        return None, f"could not reliably decode page bytes (declared={charset!r}): {exc}"


@dataclass
class RobotsCache:
    fetcher: BudgetedFetcher
    _cache: dict[str, RobotFileParser] = field(default_factory=dict)

    def allowed(self, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._cache.get(host_key)
        if rp is None:
            rp = RobotFileParser()
            robots_url = f"{host_key}/robots.txt"
            result = self.fetcher.get(robots_url)
            if result.ok and result.body is not None:
                try:
                    text = result.body.decode("utf-8", errors="replace")
                    rp.parse(text.splitlines())
                except Exception:
                    rp = None  # unparsable robots.txt: fail open, matching common crawler behaviour
            else:
                rp = None  # no robots.txt / fetch failed: fail open
            self._cache[host_key] = rp if rp is not None else _AllowAll()
            rp = self._cache[host_key]
        return rp.can_fetch(user_agent, url)


class _AllowAll:
    def can_fetch(self, *_args, **_kwargs) -> bool:
        return True


@dataclass
class PageCapture:
    url: str
    final_url: str
    ok: bool
    status: Optional[int]
    text: Optional[str]
    decode_note: str
    title: str = ""
    description: str = ""
    json_ld: list = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    content_sha256: Optional[str] = None
    redirect_chain: list[str] = field(default_factory=list)
    redirect_domain_changed: bool = False
    fetch_error_kind: Optional[str] = None


def fetch_and_parse(
    url: str,
    fetcher: BudgetedFetcher,
    *,
    requested_registered_domain: Optional[str] = None,
) -> PageCapture:
    result: FetchResult = fetcher.get(url, accept="text/html,application/xhtml+xml")

    domain_changed = False
    if requested_registered_domain and result.final_url:
        final_domain = registered_domain(result.final_url)
        if final_domain and final_domain != requested_registered_domain:
            domain_changed = True

    if not result.ok:
        return PageCapture(
            url=url, final_url=result.final_url, ok=False, status=result.status,
            text=None, decode_note="", redirect_chain=result.redirect_chain,
            redirect_domain_changed=domain_changed, fetch_error_kind=result.error_kind,
        )

    text, note = decode_page(result.headers, result.body)
    capture = PageCapture(
        url=url, final_url=result.final_url, ok=text is not None, status=result.status,
        text=text, decode_note=note, content_sha256=result.content_sha256,
        redirect_chain=result.redirect_chain, redirect_domain_changed=domain_changed,
        fetch_error_kind=None if text is not None else "decode_error",
    )
    if text is None:
        return capture

    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")

    if soup.title and soup.title.string:
        capture.title = soup.title.string.strip()
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc_tag and desc_tag.get("content"):
        capture.description = desc_tag["content"].strip()

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
            capture.json_ld.append(data)
        except Exception:
            continue

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            capture.links.append(urljoin(result.final_url, href))

    return capture


def visible_text(capture: PageCapture, *, max_chars: int = 20000) -> str:
    if not capture.text:
        return ""
    try:
        soup = BeautifulSoup(capture.text, "lxml")
    except Exception:
        soup = BeautifulSoup(capture.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def classify_links(links: list[str], site_registered_domain: str) -> dict[str, list[str]]:
    """Bucket same-host links by purpose, for prioritised secondary fetches."""
    careers, news, about, social = [], [], [], []
    social_hosts = ("linkedin.com", "facebook.com", "instagram.com", "youtube.com", "twitter.com", "x.com")
    for link in links:
        parsed = urlparse(link)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if any(host == h or host.endswith("." + h) for h in social_hosts):
            social.append(link)
            continue
        if registered_domain(link) != site_registered_domain:
            continue
        if any(path.startswith(p) for p in CAREER_PATH_HINTS):
            careers.append(link)
        elif any(path.startswith(p) for p in NEWS_PATH_HINTS):
            news.append(link)
        elif any(path.startswith(p) for p in ABOUT_PATH_HINTS):
            about.append(link)
    return {"careers": careers[:2], "news": news[:2], "about": about[:1], "social": list(dict.fromkeys(social))[:6]}


def extract_job_postings(capture: PageCapture) -> list[dict]:
    """Structured-only extraction: JSON-LD JobPosting. No free-text guessing."""
    postings = []
    for item in capture.json_ld:
        candidates = item if isinstance(item, list) else [item]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            if entry.get("@type") in ("JobPosting", ["JobPosting"]):
                postings.append({
                    "title": entry.get("title"),
                    "date_posted": entry.get("datePosted"),
                    "valid_through": entry.get("validThrough"),
                    "url": entry.get("url") or capture.final_url,
                })
    return postings


def extract_dated_articles(capture: PageCapture) -> list[dict]:
    """Structured-only dated-activity extraction: JSON-LD Article/NewsArticle
    with a datePublished, plus <time datetime="..."> elements as a fallback.
    """
    items = []
    for item in capture.json_ld:
        candidates = item if isinstance(item, list) else [item]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            if entry.get("@type") in ("Article", "NewsArticle", ["Article"], ["NewsArticle"]):
                date = entry.get("datePublished") or entry.get("dateCreated")
                if date:
                    items.append({"title": entry.get("headline") or entry.get("name"),
                                  "date": date, "url": entry.get("url") or capture.final_url})
    if not items and capture.text:
        try:
            soup = BeautifulSoup(capture.text, "lxml")
        except Exception:
            soup = BeautifulSoup(capture.text, "html.parser")
        for tag in soup.find_all("time"):
            dt = tag.get("datetime")
            if dt:
                items.append({"title": tag.get_text(strip=True) or None, "date": dt, "url": capture.final_url})
    return items[:10]
