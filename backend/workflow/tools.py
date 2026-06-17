"""Tool & data layer (§4.5) with provenance capture.

Each research subagent calls these tools. The search provider is pluggable (Tavily
default). Every scrape records the fetched text so the verifier can later ground claims
against the exact source content. Tools return plain dicts/strings (model-friendly) and
also append provenance to a per-branch collector passed in by the research node.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

import httpx

from app.core.config import get_settings


@dataclass
class ToolContext:
    """Per-branch collector for tool results and provenance."""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    fetched_sources: List[Dict[str, Any]] = field(default_factory=list)

    def record_call(self, tool: str, tool_input: Dict[str, Any], summary: str) -> None:
        self.tool_calls.append({"tool": tool, "input": tool_input, "output_summary": summary})

    def record_source(self, url: str, *, title: str = "", publisher: str = "",
                      snippet: str = "", content: str = "") -> None:
        self.fetched_sources.append({
            "url": url, "title": title, "publisher": publisher, "snippet": snippet,
            "content": content, "retrieved_at": datetime.now(timezone.utc).isoformat(),
        })


# --------------------------------------------------------------------------------------
# Search provider (pluggable; Tavily default)
# --------------------------------------------------------------------------------------
def _tavily_search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    settings = get_settings()
    if not settings.tavily_api_key:
        return []
    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=settings.tavily_api_key)
        # search_depth="advanced" + include_raw_content pulls fuller page text (richer
        # grounding for the verifier and more detail for the writer).
        resp = client.search(
            query=query,
            max_results=max_results,
            include_raw_content=settings.search_include_raw_content,
            search_depth=settings.search_depth,
        )
        out = []
        for r in resp.get("results", []):
            raw = r.get("raw_content") or ""
            out.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "content": raw[: settings.scrape_max_chars],  # full page text when available
            })
        return out
    except Exception as e:  # network/credential failures are non-fatal to the loop
        return [{"title": "", "url": "", "snippet": f"[search error] {e}", "content": ""}]


# Provider registry so the search backend is swappable.
SEARCH_PROVIDERS: Dict[str, Callable[[str, int], List[Dict[str, Any]]]] = {
    "tavily": _tavily_search,
}


def web_search(query: str, ctx: ToolContext, *, provider: str = "tavily", max_results: int | None = None) -> List[Dict[str, Any]]:
    """Search the web; record provenance (incl. full page content when available)."""
    settings = get_settings()
    n = max_results or settings.search_max_results
    fn = SEARCH_PROVIDERS.get(provider, _tavily_search)
    results = fn(query, n)
    # Store the FULL page content on the source record (for the verifier's grounding), but
    # return only a COMPACT view to the model — dumping 8×50k chars of raw content into the
    # transcript on every search is what made research crawl. The agent can scrape_url a
    # specific page if it needs the full text inline.
    compact: List[Dict[str, Any]] = []
    for r in results:
        if r.get("url"):
            ctx.record_source(
                r["url"], title=r.get("title", ""), snippet=r.get("snippet", ""),
                content=r.get("content", ""),
            )
        compact.append({"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")})
    ctx.record_call("web_search", {"query": query}, f"{len(results)} results")
    return compact


def scrape_url(url: str, ctx: ToolContext) -> Dict[str, Any]:
    """Fetch a URL, extract the main text, and store it for the verifier."""
    settings = get_settings()
    text, title = "", ""
    try:
        headers = {"User-Agent": settings.scraper_user_agent}
        with httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
        text, title = _extract_main_text(html, url)
    except Exception as e:
        ctx.record_call("scrape_url", {"url": url}, f"[scrape error] {e}")
        return {"url": url, "title": "", "text": "", "error": str(e)}

    ctx.record_source(url, title=title, content=text, snippet=text[:300])
    ctx.record_call("scrape_url", {"url": url}, f"{len(text)} chars extracted")
    return {"url": url, "title": title, "text": text}


def _extract_main_text(html: str, url: str) -> tuple[str, str]:
    title = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
        if extracted:
            text = extracted
        else:
            text = ""
    except Exception:
        text = ""
    # Title + fallback body via BeautifulSoup.
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        if not text:
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = " ".join(soup.get_text(" ").split())
    except Exception:
        pass
    return text[: get_settings().scrape_max_chars], title


def read_file(path: str, ctx: ToolContext) -> Dict[str, Any]:
    """Read an uploaded supporting document's extracted text."""
    try:
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        ctx.record_call("read_file", {"path": path}, f"{len(text)} chars")
        return {"path": path, "text": text[: get_settings().scrape_max_chars]}
    except Exception as e:
        ctx.record_call("read_file", {"path": path}, f"[read error] {e}")
        return {"path": path, "text": "", "error": str(e)}


def code_executor(code: str, ctx: ToolContext) -> Dict[str, Any]:
    """Restricted Python eval for the aggregator's quantitative consolidation.

    Executes in a namespace with no builtins beyond a safe subset. This is for
    deterministic numeric consolidation (sums, counts, sorts) — not arbitrary I/O.
    """
    safe_builtins = {
        "len": len, "sum": sum, "min": min, "max": max, "sorted": sorted,
        "round": round, "abs": abs, "range": range, "enumerate": enumerate,
        "list": list, "dict": dict, "set": set, "tuple": tuple, "float": float,
        "int": int, "str": str, "any": any, "all": all, "map": map, "filter": filter,
    }
    ns: Dict[str, Any] = {"__builtins__": safe_builtins, "result": None}
    try:
        exec(code, ns)  # noqa: S102 — sandboxed namespace, no builtins/imports
        ctx.record_call("code_executor", {"code": code[:200]}, "ok")
        return {"result": ns.get("result")}
    except Exception as e:
        ctx.record_call("code_executor", {"code": code[:200]}, f"[exec error] {e}")
        return {"result": None, "error": str(e)}


# Map tool names (as they appear in plans) to callables.
def get_tool_fns(names: List[str], ctx: ToolContext) -> Dict[str, Callable[..., Any]]:
    table: Dict[str, Callable[..., Any]] = {
        "web_search": lambda query, **kw: web_search(query, ctx, **kw),
        "scraper": lambda url, **kw: scrape_url(url, ctx),
        "scrape_url": lambda url, **kw: scrape_url(url, ctx),
        "read_file": lambda path, **kw: read_file(path, ctx),
        "file_reader": lambda path, **kw: read_file(path, ctx),
        "code_executor": lambda code, **kw: code_executor(code, ctx),
    }
    return {n: table[n] for n in names if n in table}
