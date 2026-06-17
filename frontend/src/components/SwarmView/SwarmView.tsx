// SwarmView (§8.1 Run Detail): live swarm view driven by SSE. Planner status, each
// research agent as a card, then aggregator/writer/verifier.
import { Box, Card, CardContent, Chip, CircularProgress, LinearProgress, Stack, Typography } from "@mui/material";
import { useAppSelector } from "../../app/store";

const PIPELINE = ["planner", "aggregator", "synthesizer", "verifier", "renderer"];

const statusColor = (s?: string): "default" | "info" | "success" | "error" => {
  if (s === "completed" || s === "done") return "success";
  if (s === "failed") return "error";
  if (s) return "info";
  return "default";
};

const isDone = (s?: string) => s === "completed" || s === "done";

export default function SwarmView({ runId, active = false }: { runId: string; active?: boolean }) {
  const stream = useAppSelector((s) => s.runStream.byRun[runId]);

  if (!stream) {
    // No live events yet — show a spinner while the run is active, else nothing.
    return active ? (
      <Stack direction="row" spacing={1} alignItems="center" sx={{ my: 2 }}>
        <CircularProgress size={18} />
        <Typography variant="body2" color="text.secondary">
          Waiting for live progress…
        </Typography>
        <Box sx={{ flex: 1 }}>
          <LinearProgress />
        </Box>
      </Stack>
    ) : null;
  }

  const agents = Object.values(stream.agents);

  return (
    <Box>
      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap", alignItems: "center" }}>
        {PIPELINE.map((node) => {
          const st = stream.nodes[node]?.status;
          const inProgress = active && !!stream.nodes[node] && !isDone(st);
          return (
            <Chip
              key={node}
              icon={inProgress ? <CircularProgress size={12} sx={{ ml: 1 }} /> : undefined}
              label={`${node}: ${st ?? "pending"}`}
              color={statusColor(st)}
              variant={stream.nodes[node] ? "filled" : "outlined"}
            />
          );
        })}
      </Stack>

      <Typography variant="subtitle1" gutterBottom>
        Research swarm — {agents.filter((a) => a.status === "completed").length}/{agents.length} completed
      </Typography>
      <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 2 }}>
        {agents.map((a) => {
          const done = a.status === "completed";
          // Only spin while the run is genuinely active; if the run ended, a non-completed
          // agent was interrupted (not still working).
          const running = a.status === "running" && active;
          const interrupted = !active && a.status === "running";
          return (
            <Card
              key={a.agent}
              variant="outlined"
              sx={{ opacity: a.status === "pending" ? 0.6 : 1, borderColor: running ? "primary.main" : undefined }}
            >
              <CardContent>
                <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between">
                  <Typography variant="subtitle2">{a.agent}</Typography>
                  {running ? (
                    <CircularProgress size={16} />
                  ) : (
                    <Chip
                      size="small"
                      label={interrupted ? "interrupted" : a.status}
                      color={done ? "success" : interrupted ? "warning" : "default"}
                      variant={done ? "filled" : "outlined"}
                    />
                  )}
                </Stack>
                {a.model && <Chip size="small" label={a.model} sx={{ my: 0.5 }} />}
                <Typography variant="body2" color="text.secondary">
                  findings: {a.n_findings ?? 0} · tool calls: {a.n_tool_calls ?? 0}
                </Typography>
              </CardContent>
            </Card>
          );
        })}
      </Box>

      {stream.nodes["verifier"]?.detail && (
        <Box sx={{ mt: 2 }}>
          <Typography variant="subtitle1">Verifier</Typography>
          <Typography variant="body2">
            Coverage: {((stream.nodes["verifier"].detail.citation_coverage ?? 0) * 100).toFixed(0)}% · Faithfulness:{" "}
            {((stream.nodes["verifier"].detail.faithfulness_score ?? 0) * 100).toFixed(0)}% · Flags:{" "}
            {stream.nodes["verifier"].detail.n_flags ?? 0}
          </Typography>
        </Box>
      )}
    </Box>
  );
}
