"""Headless CLI runner (Milestone 1, §13.1).

Runs the workflow core end-to-end from the terminal on the config/*.yaml definitions and
writes RAW + FINAL report JSON locally. Lets you verify citation verification without the
API/DB/frontend.

Usage:
  python cli.py --subject "Anunta Technology Management Services Limited" --type company
  python cli.py --subject "Jane Doe" --type individual --out ./out
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

# Ensure `app` and `workflow` packages import when run from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.config import get_settings, validate_required  # noqa: E402
from app.core.prompts import register_templates  # noqa: E402
from workflow.graph import build_graph, initial_state  # noqa: E402
from workflow.nodes.renderer import render_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep DD headless workflow runner")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--type", dest="subject_type", choices=["company", "individual"], required=True)
    parser.add_argument("--task", default="")
    parser.add_argument("--model", dest="global_model", default=None, help="run-level global default model")
    parser.add_argument("--out", default="./out")
    args = parser.parse_args()

    settings = get_settings()
    validate_required(settings, ["anthropic_api_key"])
    register_templates()

    model_config = {"global_default": args.global_model} if args.global_model else {}
    run_id = str(uuid.uuid4())

    graph = build_graph(checkpointer=None)
    state = initial_state(
        run_id=run_id,
        subject=args.subject,
        subject_type=args.subject_type,
        task=args.task,
        model_config=model_config,
    )

    print(f"[run {run_id}] {args.subject_type}: {args.subject}")
    final_state = graph.invoke(state, config={"recursion_limit": settings.recursion_limit})

    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = final_state.get("raw_report", {})
    final = final_state.get("final_report", {})
    (out_dir / "raw.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False))
    (out_dir / "final.json").write_text(json.dumps(final, indent=2, ensure_ascii=False))
    (out_dir / "raw.md").write_text(render_markdown(raw, "raw"))
    (out_dir / "final.md").write_text(render_markdown(final, "final"))

    v = final.get("verification", {})
    print(f"[done] cost≈${final_state.get('cost_usd', 0):.3f}  "
          f"coverage={v.get('citation_coverage', 0):.0%}  "
          f"faithfulness={v.get('faithfulness_score', 0):.0%}  "
          f"flags={len(v.get('flags', []))}")
    print(f"[out] {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
