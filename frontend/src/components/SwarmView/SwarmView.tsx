// SwarmView (§8.1 Run Detail): live swarm view driven by SSE. Planner status, each
// research agent as a card, then aggregator/writer/verifier.
import { Box, Card, CardContent, Chip, LinearProgress, Stack, Typography } from "@mui/material";
import { useAppSelector } from "../../app/store";

const PIPELINE = ["planner", "aggregator", "synthesizer", "verifier", "renderer"];

const statusColor = (s?: string): "default" | "info" | "success" | "error" => {
  if (s === "completed" || s === "done") return "success";
  if (s === "failed") return "error";
  if (s) return "info";
  return "default";
};

export default function SwarmView({ runId }: { runId: string }) {
  const stream = useAppSelector((s) => s.runStream.byRun[runId]);
  if (!stream) return <LinearProgress />;

  const agents = Object.values(stream.agents);

  return (
    <Box>
      <Typography variant="h6" gutterBottom>
        Run status: <Chip label={stream.runStatus} color={statusColor(stream.runStatus)} />
      </Typography>

      <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: "wrap" }}>
        {PIPELINE.map((node) => (
          <Chip
            key={node}
            label={`${node}: ${stream.nodes[node]?.status ?? "pending"}`}
            color={statusColor(stream.nodes[node]?.status)}
            variant={stream.nodes[node] ? "filled" : "outlined"}
          />
        ))}
      </Stack>

      <Typography variant="subtitle1" gutterBottom>
        Research swarm ({agents.length})
      </Typography>
      <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 2 }}>
        {agents.map((a) => (
          <Card key={a.agent} variant="outlined">
            <CardContent>
              <Typography variant="subtitle2">{a.agent}</Typography>
              <Chip size="small" label={a.model} sx={{ my: 0.5 }} />
              <Typography variant="body2" color="text.secondary">
                status: {a.status}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                findings: {a.n_findings ?? 0} · tool calls: {a.n_tool_calls ?? 0}
              </Typography>
            </CardContent>
          </Card>
        ))}
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
