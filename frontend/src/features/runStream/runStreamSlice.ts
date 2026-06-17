// runStreamSlice (§8.2): live SSE events keyed by run_id -> agent/node statuses.
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { AgentCard, ProgressEvent } from "../../types";

interface NodeStatus {
  status: string;
  detail?: ProgressEvent;
}

interface RunStreamState {
  byRun: Record<
    string,
    {
      events: ProgressEvent[];
      nodes: Record<string, NodeStatus>;
      agents: Record<string, AgentCard>;
      runStatus: string;
    }
  >;
}

const initialState: RunStreamState = { byRun: {} };

const runStreamSlice = createSlice({
  name: "runStream",
  initialState,
  reducers: {
    eventReceived(state, action: PayloadAction<{ runId: string; event: ProgressEvent }>) {
      const { runId, event } = action.payload;
      const entry = state.byRun[runId] ?? { events: [], nodes: {}, agents: {}, runStatus: "queued" };
      entry.events.push(event);
      if (event.node === "run" && event.status) entry.runStatus = event.status;

      // Planner announces the full research roster → seed a card per agent as "pending"
      // so the whole swarm is visible immediately (before any agent starts/finishes).
      if (event.node === "planner" && event.research_agents) {
        for (const ra of event.research_agents) {
          if (!entry.agents[ra.name]) {
            entry.agents[ra.name] = { agent: ra.name, role: ra.role, model: ra.model, status: "pending" };
          }
        }
      }

      if (event.node === "research_agent" && event.agent) {
        // Merge so a "running" event doesn't wipe seeded fields and "completed" adds counts.
        const prev = entry.agents[event.agent] ?? { agent: event.agent, status: "pending" };
        entry.agents[event.agent] = {
          ...prev,
          status: event.status ?? prev.status,
          model: event.model ?? prev.model,
          n_findings: event.n_findings ?? prev.n_findings,
          n_tool_calls: event.n_tool_calls ?? prev.n_tool_calls,
        };
      }

      if (event.node && event.node !== "research_agent") {
        entry.nodes[event.node] = { status: event.status ?? "", detail: event };
      }
      state.byRun[runId] = entry;
    },
    clearRunStream(state, action: PayloadAction<string>) {
      delete state.byRun[action.payload];
    },
  },
});

export const { eventReceived, clearRunStream } = runStreamSlice.actions;
export default runStreamSlice.reducer;
