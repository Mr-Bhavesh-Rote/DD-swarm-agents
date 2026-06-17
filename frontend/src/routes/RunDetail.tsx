// Run Detail (§8.1): live swarm view via SSE + cancel/resume + liveness indicator.
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Stack,
  Typography,
  Link as MuiLink,
} from "@mui/material";
import { useNavigate, useParams } from "react-router-dom";
import {
  useCancelRunMutation,
  useGetRunQuery,
  useGetTraceQuery,
  useResumeRunMutation,
} from "../api/runsApi";
import { useRunStream } from "../features/runStream/useRunStream";
import { useAppDispatch } from "../app/store";
import { clearRunStream } from "../features/runStream/runStreamSlice";
import SwarmView from "../components/SwarmView/SwarmView";

const TERMINAL = ["done", "failed", "cancelled"];

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

  const status = run?.status ?? "queued";
  const active = !TERMINAL.includes(status);
  // active flips false->true on resume, which re-runs the effect and reconnects the stream.
  useRunStream(id, active);

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
            color={status === "done" ? "success" : status === "failed" ? "error" : "info"}
          />
          {active &&
            (alive ? (
              <Chip color="success" size="small" variant="outlined" label={`live · ${Math.round(hbAge ?? 0)}s`} />
            ) : (
              <Chip color="warning" size="small" variant="outlined" label="no heartbeat" />
            ))}
          {active && (
            <Button color="error" variant="outlined" onClick={() => id && cancelRun(id)}>
              Cancel
            </Button>
          )}
          {(status === "failed" || status === "cancelled") && (
            <Button variant="contained" disabled={resuming} onClick={onResume}>
              {resuming ? "Resuming…" : "Resume"}
            </Button>
          )}
          {status === "done" && (
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

      {id && <SwarmView runId={id} active={active} />}
    </Box>
  );
}
