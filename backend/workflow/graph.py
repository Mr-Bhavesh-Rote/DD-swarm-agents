"""LangGraph StateGraph assembly (§4.1).

planner -> (Send fan-out) -> research_agent* -> aggregator -> synthesizer -> verifier
  verifier --(unsupported claims, budget remains)--> synthesizer   (revise loop)
  verifier --(ok / budget exhausted)--> renderer -> END
"""
from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from workflow.nodes.aggregator import aggregator_node
from workflow.nodes.planner import planner_node
from workflow.nodes.renderer import renderer_node
from workflow.nodes.research import dispatch_research, research_agent_node
from workflow.nodes.synthesizer import synthesizer_node
from workflow.nodes.verifier import route_after_verify, verifier_node
from workflow.state import GraphState


def build_graph(checkpointer: Optional[Any] = None):
    """Compile the workflow graph. Pass a checkpointer (Postgres in prod) for resumable runs."""
    g = StateGraph(GraphState)

    g.add_node("planner", planner_node)
    g.add_node("research_agent", research_agent_node)
    g.add_node("aggregator", aggregator_node)
    g.add_node("synthesizer", synthesizer_node)
    g.add_node("verifier", verifier_node)
    g.add_node("renderer", renderer_node)

    g.add_edge(START, "planner")
    # Parallel fan-out: one research_agent branch per research agent in the plan.
    g.add_conditional_edges("planner", dispatch_research, ["research_agent"])
    g.add_edge("research_agent", "aggregator")
    g.add_edge("aggregator", "synthesizer")
    g.add_edge("synthesizer", "verifier")
    # Revise loop or finalize.
    g.add_conditional_edges("verifier", route_after_verify, ["synthesizer", "renderer"])
    g.add_edge("renderer", END)

    return g.compile(checkpointer=checkpointer)


def initial_state(
    *,
    run_id: str,
    subject: str,
    subject_type: str,
    task: str = "",
    model_config: Optional[dict] = None,
    plan_override: Optional[dict] = None,
    uploaded_file_ids: Optional[list] = None,
) -> dict:
    return {
        "run_id": run_id,
        "subject": subject,
        "subject_type": subject_type,
        "task": task,
        "model_config": model_config or {},
        "plan_override": plan_override,
        "uploaded_file_ids": uploaded_file_ids or [],
        "raw_outputs": [],
        "findings": [],
        "sources_raw": [],
        "model_summary": {},
        "cost_usd": 0.0,
        "events": [],
        "revision_count": 0,
    }
