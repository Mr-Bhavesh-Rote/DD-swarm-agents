// New Run (§8.1): subject_type toggle, subject, task (prefilled per type), planning mode,
// ModelSelector, file upload. Submit -> POST /api/runs, optimistic redirect to Run Detail (§8.3).
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Slider,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import AutoFixHighIcon from "@mui/icons-material/AutoFixHigh";
import { useNavigate } from "react-router-dom";
import { useCreateRunMutation, useRefineTaskMutation } from "../api/runsApi";
import { useAppDispatch, useAppSelector } from "../app/store";
import {
  setMaxResearchAgents,
  setPlanningMode,
  setSubject,
  setSubjectType,
  setTask,
} from "../features/runForm/runFormSlice";
import ModelSelector from "../components/ModelSelector/ModelSelector";
import type { PlanningMode, SubjectType } from "../types";

const SYSTEM_MAX_AGENTS = 6; // mirrors backend MAX_SUBAGENTS default

export default function NewRun() {
  const nav = useNavigate();
  const dispatch = useAppDispatch();
  const form = useAppSelector((s) => s.runForm);
  const [createRun, { isLoading }] = useCreateRunMutation();
  const [refineTask, { isLoading: isRefining, error: refineError }] = useRefineTaskMutation();

  const submit = async () => {
    const res = await createRun({
      subject_type: form.subject_type,
      subject: form.subject,
      task: form.task,
      model_config: form.model_config,
      plan_override: form.plan_override,
      uploaded_file_ids: form.uploaded_file_ids,
      planning_mode: form.planning_mode,
      max_research_agents: form.planning_mode === "ai" ? form.max_research_agents : null,
    }).unwrap();
    nav(`/runs/${res.run_id}`); // optimistic redirect + attach to stream
  };

  const refine = async () => {
    const res = await refineTask({
      subject_type: form.subject_type,
      subject: form.subject,
      query: form.task, // expand whatever is in the box (a plain-English ask is fine)
    }).unwrap();
    dispatch(setTask(res.task)); // editable before submit
  };

  const isAi = form.planning_mode === "ai";

  return (
    <Box sx={{ maxWidth: 760, mx: "auto" }}>
      <Typography variant="h4" gutterBottom>
        New Run
      </Typography>
      <Card>
        <CardContent sx={{ display: "grid", gap: 2 }}>
          <ToggleButtonGroup
            exclusive
            value={form.subject_type}
            onChange={(_e, v: SubjectType | null) => v && dispatch(setSubjectType(v))}
          >
            <ToggleButton value="company">Company</ToggleButton>
            <ToggleButton value="individual">Individual</ToggleButton>
          </ToggleButtonGroup>

          <TextField
            label="Subject"
            value={form.subject}
            onChange={(e) => dispatch(setSubject(e.target.value))}
            placeholder="e.g. Anunta Technology Management Services Limited"
          />

          <Box>
            <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 0.5 }}>
              <Typography variant="subtitle2">Task / instructions</Typography>
              <Button
                size="small"
                startIcon={<AutoFixHighIcon fontSize="small" />}
                onClick={refine}
                disabled={isRefining || !form.task.trim()}
              >
                {isRefining ? "Refining…" : "Refine with AI"}
              </Button>
            </Stack>
            <TextField
              fullWidth
              value={form.task}
              onChange={(e) => dispatch(setTask(e.target.value))}
              multiline
              minRows={5}
              placeholder="Describe what you want, or edit the section list. 'Refine with AI' turns a plain-English ask into a structured task."
            />
            {refineError ? (
              <Alert severity="error" sx={{ mt: 1 }}>
                Couldn’t refine the task — edit it manually and continue.
              </Alert>
            ) : null}
          </Box>

          <Box>
            <Typography variant="subtitle1" gutterBottom>
              Planning mode
            </Typography>
            <ToggleButtonGroup
              exclusive
              value={form.planning_mode}
              onChange={(_e, v: PlanningMode | null) => v && dispatch(setPlanningMode(v))}
            >
              <ToggleButton value="template">Standard</ToggleButton>
              <ToggleButton value="ai">AI-tailored</ToggleButton>
            </ToggleButtonGroup>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
              {isAi
                ? "An orchestrator model reads your task and builds a custom research swarm. More flexible; costs an extra planning call and can run more agents."
                : "Deterministic template swarm for this subject type — cheapest and most predictable. The task still drives the report sections."}
            </Typography>
          </Box>

          {isAi ? (
            <Box>
              <Typography variant="subtitle2" gutterBottom>
                Max research agents:{" "}
                {form.max_research_agents ?? `system default (${SYSTEM_MAX_AGENTS})`}
              </Typography>
              <Slider
                value={form.max_research_agents ?? SYSTEM_MAX_AGENTS}
                onChange={(_e, v) => dispatch(setMaxResearchAgents(v as number))}
                min={1}
                max={SYSTEM_MAX_AGENTS}
                step={1}
                marks
                valueLabelDisplay="auto"
              />
              <Typography variant="caption" color="text.secondary">
                Caps the swarm size — the main driver of run cost. Lower = cheaper, fewer parallel
                research branches.
              </Typography>
            </Box>
          ) : null}

          <Box>
            <Typography variant="subtitle1" gutterBottom>
              Model selection
            </Typography>
            <ModelSelector />
          </Box>

          <Button variant="contained" disabled={!form.subject || isLoading} onClick={submit}>
            Start research
          </Button>
        </CardContent>
      </Card>
    </Box>
  );
}
