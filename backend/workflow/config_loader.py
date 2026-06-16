"""Config loader (§5.7): YAML (canonical) / legacy JSON -> normalized WorkflowPlan.

Primary format is the CrewAY-style agents.*.yaml + tasks.*.yaml pair. A legacy `.json`
plan is accepted on import only (so originally-provided JSON workflows still load).
Validation fails fast on unknown agent references, cycles in depends_on, or unknown
tool names.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from app.schemas.contracts import AgentSpec, WorkflowPlan

KNOWN_TOOLS = {"web_search", "scraper", "scrape_url", "read_file", "file_reader", "code_executor"}


class ConfigError(ValueError):
    """Raised on any invalid configuration; carries a clear, fail-fast message."""


def _config_dir() -> Path:
    # repo_root/config  (this file is repo_root/backend/workflow/config_loader.py)
    return Path(__file__).resolve().parents[2] / "config"


def load_plan_for_subject(subject_type: str, task: str = "", config_dir: Path | None = None) -> WorkflowPlan:
    """Load and normalize the agents/tasks YAML pair for a subject type."""
    base = config_dir or _config_dir()
    agents_path = _first_existing(base, [f"agents.{subject_type}.yaml", f"agents.{subject_type}.yml"])
    tasks_path = _first_existing(base, [f"tasks.{subject_type}.yaml", f"tasks.{subject_type}.yml"])
    if not agents_path or not tasks_path:
        raise ConfigError(
            f"No YAML config found for subject_type='{subject_type}' under {base} "
            f"(expected agents.{subject_type}.yaml + tasks.{subject_type}.yaml)."
        )
    agents_raw = _safe_load_yaml(agents_path)
    tasks_raw = _safe_load_yaml(tasks_path)
    return _merge_to_plan(agents_raw, tasks_raw, task=task)


def load_plan_from_file(path: str | Path, task: str = "") -> WorkflowPlan:
    """Import a legacy single-file plan: `.json` (legacy export) or a combined YAML."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        return _normalize_plan_dict(data, task=task)
    data = yaml.safe_load(text)
    # Combined YAML may carry top-level `agents:`/`tasks:` or already be a WorkflowPlan.
    if isinstance(data, dict) and "agents" in data and isinstance(data["agents"], list):
        return _normalize_plan_dict(data, task=task)
    if isinstance(data, dict) and "agents" in data and "tasks" in data:
        return _merge_to_plan(data["agents"], data["tasks"], task=task)
    raise ConfigError(f"Unrecognized plan file format: {p}")


def normalize_plan(data: Dict[str, Any]) -> WorkflowPlan:
    """Round-trip a JSON WorkflowPlan from the wire (PUT /api/runs/{id}/plan)."""
    return _normalize_plan_dict(data, task=data.get("task", ""))


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------
def _first_existing(base: Path, names: List[str]) -> Path | None:
    for n in names:
        if (base / n).exists():
            return base / n
    return None


def _safe_load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping at the top level.")
    return data


def _merge_to_plan(agents_raw: Dict[str, Any], tasks_raw: Dict[str, Any], task: str) -> WorkflowPlan:
    agents: List[AgentSpec] = []
    task_name_to_agent: Dict[str, str] = {}

    # Resolve each task -> its agent definition; carry depends_on / expected_output.
    for task_name, tdef in (tasks_raw or {}).items():
        tdef = tdef or {}
        agent_name = tdef.get("agent")
        if not agent_name:
            raise ConfigError(f"Task '{task_name}' has no `agent`.")
        if agent_name not in agents_raw:
            raise ConfigError(
                f"Task '{task_name}' references unknown agent '{agent_name}'. "
                f"Known agents: {sorted(agents_raw)}"
            )
        task_name_to_agent[task_name] = agent_name

    # depends_on in tasks is expressed in task names; translate to agent names.
    specs_by_name: Dict[str, AgentSpec] = {}
    for task_name, tdef in (tasks_raw or {}).items():
        tdef = tdef or {}
        agent_name = task_name_to_agent[task_name]
        adef = agents_raw[agent_name] or {}
        tools = adef.get("tools", ["web_search", "scraper"])
        _validate_tools(agent_name, tools)
        dep_tasks = tdef.get("depends_on", []) or []
        dep_agents = []
        for dt in dep_tasks:
            if dt not in task_name_to_agent:
                raise ConfigError(f"Task '{task_name}' depends_on unknown task '{dt}'.")
            dep_agents.append(task_name_to_agent[dt])
        spec = AgentSpec(
            name=agent_name,
            role=adef.get("role", agent_name),
            goal=adef.get("goal", ""),
            rationale=tdef.get("expected_output", ""),
            depends_on=dep_agents,
            max_iterations=int(adef.get("max_iterations", 10)),
            suggested_tools=list(tools),
            model=adef.get("model"),
            provider=adef.get("provider", "anthropic"),
            credential_id=adef.get("credential_id"),
        )
        specs_by_name[agent_name] = spec

    agents = list(specs_by_name.values())
    _detect_cycles(agents)
    summary = f"Auto-loaded plan with {len(agents)} agents for the configured subject type."
    return WorkflowPlan(task=task, summary=summary, execution_notes="", agents=agents)


def _normalize_plan_dict(data: Dict[str, Any], task: str) -> WorkflowPlan:
    raw_agents = data.get("agents", [])
    specs: List[AgentSpec] = []
    for a in raw_agents:
        tools = a.get("suggested_tools") or a.get("tools") or ["web_search", "scraper"]
        _validate_tools(a.get("name", "?"), tools)
        specs.append(
            AgentSpec(
                name=a["name"],
                role=a.get("role", a["name"]),
                goal=a.get("goal", ""),
                rationale=a.get("rationale", a.get("expected_output", "")),
                depends_on=a.get("depends_on", []) or [],
                max_iterations=int(a.get("max_iterations", 10)),
                suggested_tools=list(tools),
                model=a.get("model"),
                provider=a.get("provider", "anthropic"),
                credential_id=a.get("credential_id"),
            )
        )
    names = {s.name for s in specs}
    for s in specs:
        for dep in s.depends_on:
            if dep not in names:
                raise ConfigError(f"Agent '{s.name}' depends_on unknown agent '{dep}'.")
    _detect_cycles(specs)
    return WorkflowPlan(
        task=task or data.get("task", ""),
        summary=data.get("summary", ""),
        execution_notes=data.get("execution_notes", ""),
        agents=specs,
    )


def _validate_tools(agent_name: str, tools: Any) -> None:
    if not isinstance(tools, list):
        raise ConfigError(f"Agent '{agent_name}' tools must be a list.")
    for t in tools:
        if t not in KNOWN_TOOLS:
            raise ConfigError(
                f"Agent '{agent_name}' references unknown tool '{t}'. Known: {sorted(KNOWN_TOOLS)}"
            )


def _detect_cycles(specs: List[AgentSpec]) -> None:
    graph = {s.name: set(s.depends_on) for s in specs}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def visit(n: str, stack: List[str]) -> None:
        color[n] = GRAY
        for m in graph.get(n, ()):
            if color.get(m) == GRAY:
                raise ConfigError(f"Cycle in depends_on: {' -> '.join(stack + [m])}")
            if color.get(m, WHITE) == WHITE:
                visit(m, stack + [m])
        color[n] = BLACK

    for n in graph:
        if color[n] == WHITE:
            visit(n, [n])
