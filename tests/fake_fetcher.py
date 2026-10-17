"""A canned, in-memory stand-in for BudgetedFetcher, used by tests so the
whole pipeline can be exercised deterministically with no network access.
Mirrors the reference starter kit's SnapshotFetcher/FetchResult pattern.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from signalpost_agent.http_client import FetchResult


@dataclass
class Canned:
    status: Optional[int] = 200
    body: Optional[bytes] = None
    ok: bool = True
    error_kind: Optional[str] = None
    error_detail: str = ""
    final_url: Optional[str] = None
    redirect_chain: list = field(default_factory=list)
    declared_length_mismatch: bool = False  # simulate truncation


class FakeFetcher:
    """Drop-in replacement for BudgetedFetcher.get(). Configure canned
    responses by exact URL. Any URL not registered returns a 404-like
    not-found result (so tests are explicit about what they stub).
    """

    def __init__(self, responses: dict[str, Canned] | None = None):
        self.responses = responses or {}
        self.sequences: dict[str, list[Canned]] = {}
        self.calls: list[str] = []

    def add(self, url: str, canned: Canned) -> None:
        self.responses[url] = canned

    def add_sequence(self, url: str, canned_list: list[Canned]) -> None:
        """Registers a list of responses for one URL, returned in order on
        successive calls (the last one repeats once the list is exhausted).
        Used to simulate the real, observed behaviour of a transient 404
        followed by a successful response on retry.
        """
        self.sequences[url] = list(canned_list)

    def get(self, url: str, *, accept: str | None = None) -> FetchResult:
        self.calls.append(url)
        if url in self.sequences and self.sequences[url]:
            canned = self.sequences[url].pop(0) if len(self.sequences[url]) > 1 else self.sequences[url][0]
            return self._result_from_canned(url, canned)
        canned = self.responses.get(url)
        return self._result_from_canned(url, canned)

    def _result_from_canned(self, url: str, canned: Optional[Canned]) -> FetchResult:
        if canned is None:
            return FetchResult(url=url, final_url=url, status=404, ok=False,
                                error_kind="http_error", error_detail="HTTP 404 (unstubbed)")

        if canned.error_kind is not None:
            return FetchResult(url=url, final_url=canned.final_url or url,
                                status=canned.status, ok=False,
                                error_kind=canned.error_kind, error_detail=canned.error_detail,
                                redirect_chain=canned.redirect_chain)

        if not canned.ok:
            return FetchResult(url=url, final_url=canned.final_url or url,
                                status=canned.status, ok=False,
                                error_kind="http_error", error_detail=f"HTTP {canned.status}",
                                redirect_chain=canned.redirect_chain)

        body = canned.body or b""
        digest = hashlib.sha256(body).hexdigest()
        return FetchResult(
            url=url, final_url=canned.final_url or url, status=canned.status, ok=True,
            body=body, headers={}, content_sha256=digest,
            redirect_chain=canned.redirect_chain,
        )
