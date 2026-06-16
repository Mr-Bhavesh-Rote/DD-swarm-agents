// runStreamSlice (§8.2): live SSE events keyed by run_id -> agent/node statuses.
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { ProgressEvent } from "../../types";

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
      agents: Record<string, ProgressEvent>;
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
      if (event.node === "research_agent" && event.agent) {
        entry.agents[event.agent] = event;
      } else if (event.node) {
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
