// Run Detail (§8.1): live swarm view via SSE + cancel/resume + liveness indicator.
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  Typography,
  Link as MuiLink,
} from "@mui/material";
import { useNavigate, useParams } from "react-router-dom";
import {
  useApprovePlanMutation,
  useCancelRunMutation,
  useGetPlanQuery,
  useGetRunQuery,
  useGetTraceQuery,
  useResumeRunMutation,
} from "../api/runsApi";
import { useRunStream } from "../features/runStream/useRunStream";
import { useAppDispatch } from "../app/store";
import { clearRunStream } from "../features/runStream/runStreamSlice";
import SwarmView from "../components/SwarmView/SwarmView";

const TERMINAL = ["done", "needs_review", "failed", "cancelled"];
const RESEARCH_TOOLS = ["web_search", "scraper"];

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const dispatch = useAppDispatch();
  // Poll the DB status — this is the authoritative lifecycle source (the SSE stream only
  // feeds live per-agent detail and can lag/close, so it must NOT override this).
  const { data: run } = useGetRunQuery(id!, { pollingInterval: 3000 });
  const { data: trace } = useGetTraceQuery(id!);
  const [cancelRun] = useCancelRunMutation();
  const [resumeRun, { isLoading: resuming }] = useResumeRunMutation();
  const [approvePlan, { isLoading: approving }] = useApprovePlanMutation();

  const status = run?.status ?? "queued";
  const awaitingPlan = status === "awaiting_plan";
  // awaiting_plan is non-terminal but pre-research: no worker is driving it, so don't treat
  // it as "active" (no spinner / SSE stream) — show the plan-approval gate instead.
  const active = !TERMINAL.includes(status) && !awaitingPlan;
  // active flips false->true on resume, which re-runs the effect and reconnects the stream.
  useRunStream(id, active);

  // Only fetch the plan for the approval gate (avoids an extra request on normal runs).
  const { data: plan } = useGetPlanQuery(id!, { skip: !awaitingPlan });
  const researchAgents = (plan?.agents ?? []).filter((a) =>
    a.suggested_tools?.some((t) => RESEARCH_TOOLS.includes(t)),
  );

  const alive = run?.alive;
  const hbAge = run?.heartbeat_age;
  const stalled = active && alive === false;

  const onResume = async () => {
    if (!id) return;
    dispatch(clearRunStream(id)); // reset stale agent cards / status before re-running
    await resumeRun(id);
  };

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h4">{run?.subject}</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          {active && <CircularProgress size={20} thickness={5} />}
          <Chip
            label={status}
            color={
              status === "done"
                ? "success"
                : status === "failed"
                  ? "error"
                  : status === "needs_review"
                    ? "warning"
                    : "info"
            }
          />
          {run?.cost_usd != null && (
            <Chip
              size="small"
              variant="outlined"
              label={`$${run.cost_usd.toFixed(4)}`}
              title={awaitingPlan ? "Planning cost so far (research not yet started)" : "Run cost so far"}
            />
          )}
          {active &&
            (alive ? (
              <Chip color="success" size="small" variant="outlined" label={`live · ${Math.round(hbAge ?? 0)}s`} />
            ) : (
              <Chip color="warning" size="small" variant="outlined" label="no heartbeat" />
            ))}
          {(active || awaitingPlan) && (
            <Button color="error" variant="outlined" onClick={() => id && cancelRun(id)}>
              Cancel
            </Button>
          )}
          {awaitingPlan && (
            <Button variant="contained" disabled={approving} onClick={() => id && approvePlan(id)}>
              {approving ? "Starting…" : "Approve & start research"}
            </Button>
          )}
          {(status === "failed" || status === "cancelled") && (
            <Button variant="contained" disabled={resuming} onClick={onResume}>
              {resuming ? "Resuming…" : "Resume"}
            </Button>
          )}
          {(status === "done" || status === "needs_review") && (
            <Button variant="contained" onClick={() => nav(`/runs/${id}/report`)}>
              View report
            </Button>
          )}
        </Stack>
      </Stack>

      {stalled && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          This run shows an active status but no worker is updating it (heartbeat
          {hbAge != null ? ` last seen ${Math.round(hbAge)}s ago` : " missing"}). The worker may have
          stopped or lost network. It will be marked failed automatically; you can then{" "}
          <strong>Resume</strong> from the last checkpoint.
        </Alert>
      )}
      {status === "failed" && run?.error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {run.error} — use <strong>Resume</strong> to continue from the last checkpoint.
        </Alert>
      )}

      {trace?.trace_url && (
        <Typography variant="body2" sx={{ mb: 2 }}>
          Langfuse trace:{" "}
          <MuiLink href={trace.trace_url} target="_blank" rel="noopener noreferrer">
            {trace.trace_url}
          </MuiLink>
        </Typography>
      )}

      {awaitingPlan ? (
        <Card variant="outlined">
          <CardContent>
            <Alert severity="info" sx={{ mb: 2 }}>
              This AI-tailored plan was generated from your task. Review the research swarm below,
              then <strong>Approve &amp; start research</strong> — no research runs (or cost) until you do.
            </Alert>
            <Typography variant="h6" gutterBottom>
              Proposed research swarm ({researchAgents.length} agent
              {researchAgents.length === 1 ? "" : "s"})
            </Typography>
            {plan?.summary && (
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                {plan.summary}
              </Typography>
            )}
            <Stack divider={<Divider flexItem />} spacing={1.5}>
              {researchAgents.map((a) => (
                <Box key={a.name}>
                  <Typography variant="subtitle2">
                    {a.name}
                    {a.role ? ` · ${a.role}` : ""}
                  </Typography>
                  {a.goal && (
                    <Typography variant="body2" color="text.secondary">
                      {a.goal}
                    </Typography>
                  )}
                </Box>
              ))}
            </Stack>
          </CardContent>
        </Card>
      ) : (
        id && <SwarmView runId={id} active={active} />
      )}
    </Box>
  );
}
