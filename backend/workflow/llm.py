"""Shared LLM invocation helpers used by the graph nodes.

Centralizes: JSON-mode parsing, retries with backoff, and rough cost accounting so every
node behaves identically. Cost is approximate (per-1M-token published rates) and is used
only for the soft per-run budget guardrail (§10) — never for billing.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from workflow.models import make_chat_model

# Approximate USD per 1M tokens (input, output). Used only for the soft budget guard.
_COST_PER_MTOK = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-fable-5": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
}


def estimate_cost(model_id: str, in_tokens: int, out_tokens: int) -> float:
    cin, cout = _COST_PER_MTOK.get(model_id, (3.0, 15.0))
    return (in_tokens / 1_000_000) * cin + (out_tokens / 1_000_000) * cout


def extract_list(data: Any, key: str) -> list:
    """Pull a list out of an LLM JSON response, tolerant of shape.

    Models sometimes return the bare array (``[...]``) instead of the agreed
    ``{"<key>": [...]}`` object. Both are accepted here; anything else yields ``[]`` —
    so callers never crash with ``'list' object has no attribute 'get'``.
    """
    if isinstance(data, dict):
        v = data.get(key, [])
        return v if isinstance(v, list) else []
    if isinstance(data, list):
        return data
    return []


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model response."""
    text = text.strip()
    # Strip markdown fences.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Grab the first balanced {...} or [...] block.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    raise ValueError("No parseable JSON found in model output.")


def invoke_json(
    model_id: str,
    system_prompt: str,
    user_content: str,
    *,
    callbacks: Optional[List[Any]] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    retries: int = 2,
) -> Dict[str, Any]:
    """Invoke a Claude model and parse a JSON object response. Returns
    {"data": <parsed>, "cost_usd": float, "raw": str}.
    Retries with backoff on transient errors (§10 reliability)."""
    llm = make_chat_model(model_id, temperature=temperature, max_tokens=max_tokens)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]
    cfg: Dict[str, Any] = {}
    if callbacks:
        cfg["callbacks"] = callbacks

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = llm.invoke(messages, config=cfg or None)
            text = resp.content if isinstance(resp.content, str) else _stringify(resp.content)
            usage = getattr(resp, "usage_metadata", None) or {}
            cost = estimate_cost(
                model_id, usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            )
            try:
                data = _extract_json(text)
            except ValueError:
                # One corrective re-ask for strict JSON.
                if attempt < retries:
                    messages.append(HumanMessage(content="Return ONLY a valid JSON object, nothing else."))
                    continue
                data = {}
            return {"data": data, "cost_usd": cost, "raw": text}
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
    raise RuntimeError(f"LLM invocation failed after {retries + 1} attempts: {last_err}")


def _stringify(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)
