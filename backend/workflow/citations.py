"""Global citation registry (§4.2).

Dedupe sources by canonical URL; assign stable integer ids [1], [2], ... in order of
first appearance across all agents. The registry is the single authority for the id->url
mapping used by both the verifier and the hyperlinked renderer.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit


def canonical_url(url: str) -> str:
    """Normalize a URL for dedup: lowercase scheme/host, drop fragment, strip trailing slash."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        scheme = (parts.scheme or "https").lower()
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, parts.query, ""))
    except Exception:
        return url.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


class CitationRegistry:
    """Assigns stable [n] ids to sources, deduped by canonical URL."""

    def __init__(self) -> None:
        self._by_canon: Dict[str, dict] = {}
        self._next_id = 1

    def add(
        self,
        url: str,
        *,
        title: str = "",
        publisher: str = "",
        snippet: str = "",
        content: str = "",
        retrieved_at: Optional[str] = None,
    ) -> int:
        """Register a source (or return the existing id) and return its citation id."""
        canon = canonical_url(url)
        if not canon:
            return 0
        if canon in self._by_canon:
            existing = self._by_canon[canon]
            # Enrich an existing record with content/title if it was empty before.
            if content and not existing.get("content"):
                existing["content"] = content
                existing["content_hash"] = content_hash(content)
            if title and not existing.get("title"):
                existing["title"] = title
            return existing["id"]
        cid = self._next_id
        self._next_id += 1
        self._by_canon[canon] = {
            "id": cid,
            "url": url,
            "title": title,
            "publisher": publisher or _publisher_from_url(url),
            "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(),
            "snippet": snippet,
            "content": content,
            "content_hash": content_hash(content),
        }
        return cid

    def id_for_url(self, url: str) -> Optional[int]:
        rec = self._by_canon.get(canonical_url(url))
        return rec["id"] if rec else None

    def get(self, cid: int) -> Optional[dict]:
        for rec in self._by_canon.values():
            if rec["id"] == cid:
                return rec
        return None

    def sources(self, *, include_content: bool = True) -> List[dict]:
        out = sorted(self._by_canon.values(), key=lambda r: r["id"])
        if include_content:
            return [dict(r) for r in out]
        return [{k: v for k, v in r.items() if k != "content"} for r in out]


def _publisher_from_url(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""
