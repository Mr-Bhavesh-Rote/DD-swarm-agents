// SSE subscription hook (§8.2): subscribes to /api/runs/:id/stream and dispatches
// progress events into runStreamSlice. EventSource cannot send auth headers, so the
// token is passed as a query param fallback; the backend also accepts the bearer header
// for fetch-based clients.
import { useEffect } from "react";
import { useAppDispatch } from "../../app/store";
import { streamUrl } from "../../api/runsApi";
import { eventReceived } from "./runStreamSlice";
import type { ProgressEvent } from "../../types";

export function useRunStream(runId: string | undefined, active: boolean) {
  const dispatch = useAppDispatch();

  useEffect(() => {
    if (!runId || !active) return;
    const token = localStorage.getItem("token") ?? "";
    const url = `${streamUrl(runId)}?token=${encodeURIComponent(token)}`;
    const es = new EventSource(url);

    const handler = (e: MessageEvent) => {
      try {
        const event = JSON.parse(e.data) as ProgressEvent;
        dispatch(eventReceived({ runId, event }));
        if (event.node === "run" && ["done", "failed", "cancelled"].includes(event.status ?? "")) {
          es.close();
        }
      } catch {
        /* ignore malformed frames */
      }
    };

    es.onmessage = handler;
    // Named events (event: <node>) also arrive; listen broadly.
    ["planner", "research_agent", "aggregator", "synthesizer", "verifier", "renderer", "run", "progress"].forEach(
      (name) => es.addEventListener(name, handler as EventListener),
    );
    es.onerror = () => es.close();

    return () => es.close();
  }, [runId, active, dispatch]);
}
