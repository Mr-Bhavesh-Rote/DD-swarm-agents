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
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        with httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "application/pdf" in content_type or url.lower().endswith(".pdf"):
                text, title = _extract_pdf_text(resp.content, url)
            else:
                text, title = _extract_main_text(resp.text, url)
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


def _extract_pdf_text(content: bytes, url: str) -> tuple[str, str]:
    """Extract text from a PDF binary response."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=content, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        text = "\n\n".join(pages)
        title = os.path.basename(url).replace(".pdf", "").replace("-", " ").replace("_", " ")
        return text[: get_settings().scrape_max_chars], title
    except ImportError:
        # Fallback: try pdfminer
        try:
            from io import BytesIO
            from pdfminer.high_level import extract_text as pdfminer_extract
            text = pdfminer_extract(BytesIO(content))
            title = os.path.basename(url).replace(".pdf", "").replace("-", " ").replace("_", " ")
            return text[: get_settings().scrape_max_chars], title
        except Exception:
            return "", ""
    except Exception:
        return "", ""


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


# --------------------------------------------------------------------------------------
# Compliance-source tools (§4.5). Each required source gets a dedicated tool name so the
# agent config and completion gate can code-enforce that specific databases were queried.
# The implementations fall back to targeted web search + scrape where no public API is
# available, but the tool name and provenance record make coverage auditable.
# --------------------------------------------------------------------------------------
def _site_search(query: str, site: str, ctx: ToolContext, *, max_results: int = 5) -> List[Dict[str, Any]]:
    """Run a site-targeted web search and scrape the top results for full text.

    Only scrapes a URL if the web_search (Tavily) didn't already return content for it,
    avoiding redundant fetches and reducing latency.
    """
    site_query = f"site:{site} {query}"
    results = web_search(site_query, ctx, max_results=max_results)
    # Build a set of URLs that already have content from Tavily's raw_content.
    urls_with_content = {
        s["url"] for s in ctx.fetched_sources
        if s.get("content") and len(s["content"]) > 100
    }
    out: List[Dict[str, Any]] = []
    for r in results:
        url = r.get("url")
        if not url:
            continue
        # Only scrape if Tavily didn't return meaningful content for this URL.
        if url not in urls_with_content:
            detail = scrape_url(url, ctx)
            out.append({
                "title": r.get("title", detail.get("title", "")),
                "url": url,
                "snippet": r.get("snippet", detail.get("snippet", "")),
                "text": detail.get("text", ""),
            })
        else:
            # Content already captured by web_search — just return the snippet.
            out.append({
                "title": r.get("title", ""),
                "url": url,
                "snippet": r.get("snippet", ""),
                "text": r.get("snippet", ""),
            })
    return out


def ofac_sdn_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query OFAC SDN / Consolidated sanctions lists (treasury.gov)."""
    results = _site_search(name, "treasury.gov", ctx, max_results=5)
    hit = any("sanction" in (r.get("snippet") or "").lower() or "sdn" in (r.get("snippet") or "").lower() for r in results)
    ctx.record_call("ofac_sdn_search", {"name": name}, f"{len(results)} treasury results, match_hint={hit}")
    return {"source": "OFAC/Treasury", "results": results, "match_hint": hit}


def ofac_nonsdn_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query OFAC non-SDN lists (treasury.gov)."""
    results = _site_search(name, "treasury.gov", ctx, max_results=5)
    ctx.record_call("ofac_nonsdn_search", {"name": name}, f"{len(results)} treasury results")
    return {"source": "OFAC Non-SDN", "results": results}


def bis_entity_list_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query BIS Entity List / Denied Persons / Unverified lists (commerce.gov)."""
    results = _site_search(name, "commerce.gov", ctx, max_results=5)
    hit = any("entity list" in (r.get("snippet") or "").lower() for r in results)
    ctx.record_call("bis_entity_list_search", {"name": name}, f"{len(results)} commerce results, match_hint={hit}")
    return {"source": "BIS Entity List", "results": results, "match_hint": hit}


def un_sanctions_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query UN Security Council sanctions (un.org)."""
    results = _site_search(name, "un.org", ctx, max_results=5)
    ctx.record_call("un_sanctions_search", {"name": name}, f"{len(results)} un results")
    return {"source": "UN Sanctions", "results": results}


def eu_sanctions_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query EU sanctions (sanctionsmap.eu)."""
    results = _site_search(name, "sanctionsmap.eu", ctx, max_results=5)
    ctx.record_call("eu_sanctions_search", {"name": name}, f"{len(results)} eu sanctions results")
    return {"source": "EU Sanctions", "results": results}


def fpds_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query USA federal procurement (FPDS.gov) for contracts with the subject."""
    results = _site_search(name, "fpds.gov", ctx, max_results=5)
    ctx.record_call("fpds_search", {"name": name}, f"{len(results)} fpds results")
    return {"source": "FPDS", "results": results}


def usaspending_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query USAspending.gov for federal awards."""
    results = _site_search(name, "usaspending.gov", ctx, max_results=5)
    ctx.record_call("usaspending_search", {"name": name}, f"{len(results)} usaspending results")
    return {"source": "USASpending", "results": results}


def occrp_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query OCCRP / Aleph for investigations."""
    results = _site_search(name, "occrp.org", ctx, max_results=5)
    ctx.record_call("occrp_search", {"name": name}, f"{len(results)} occrp results")
    return {"source": "OCCRP", "results": results}


def epa_echo_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query EPA ECHO for enforcement/compliance records."""
    results = _site_search(name, "echo.epa.gov", ctx, max_results=5)
    ctx.record_call("epa_echo_search", {"name": name}, f"{len(results)} echo results")
    return {"source": "EPA ECHO", "results": results}


def osha_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query OSHA enforcement (osha.gov)."""
    results = _site_search(name, "osha.gov", ctx, max_results=5)
    ctx.record_call("osha_search", {"name": name}, f"{len(results)} osha results")
    return {"source": "OSHA", "results": results}


def who_profits_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query Who Profits for corporate involvement in occupation/settlements."""
    results = _site_search(name, "whoprofits.org", ctx, max_results=5)
    ctx.record_call("who_profits_search", {"name": name}, f"{len(results)} whoprofits results")
    return {"source": "Who Profits", "results": results}


def violation_tracker_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query Violation Tracker (goodjobsfirst.org) for corporate misconduct."""
    results = _site_search(name, "goodjobsfirst.org", ctx, max_results=5)
    ctx.record_call("violation_tracker_search", {"name": name}, f"{len(results)} violation tracker results")
    return {"source": "Violation Tracker", "results": results}


def pacer_search(name: str, ctx: ToolContext) -> Dict[str, Any]:
    """Query PACER / federal court records (pacer.uscourts.gov). Public search is limited; this
    searches the PACER site and related court listings."""
    results = _site_search(name, "pacer.uscourts.gov", ctx, max_results=5)
    ctx.record_call("pacer_search", {"name": name}, f"{len(results)} pacer results")
    return {"source": "PACER", "results": results}


COMPLIANCE_TOOL_FNS = {
    "ofac_sdn_search": ofac_sdn_search,
    "ofac_nonsdn_search": ofac_nonsdn_search,
    "bis_entity_list_search": bis_entity_list_search,
    "un_sanctions_search": un_sanctions_search,
    "eu_sanctions_search": eu_sanctions_search,
    "fpds_search": fpds_search,
    "usaspending_search": usaspending_search,
    "occrp_search": occrp_search,
    "epa_echo_search": epa_echo_search,
    "osha_search": osha_search,
    "who_profits_search": who_profits_search,
    "violation_tracker_search": violation_tracker_search,
    "pacer_search": pacer_search,
}


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
    for name, fn in COMPLIANCE_TOOL_FNS.items():
        table[name] = lambda f=fn, **kw: f(ctx=ctx, **kw)
    return {n: table[n] for n in names if n in table}
