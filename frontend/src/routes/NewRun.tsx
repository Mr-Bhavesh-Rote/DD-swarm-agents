// New Run (§8.1): subject_type toggle, subject, task (prefilled per type), ModelSelector,
// file upload. Submit -> POST /api/runs, optimistic redirect to Run Detail (§8.3).
import {
  Box,
  Button,
  Card,
  CardContent,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import { useNavigate } from "react-router-dom";
import { useCreateRunMutation } from "../api/runsApi";
import { useAppDispatch, useAppSelector } from "../app/store";
import { setSubject, setSubjectType, setTask } from "../features/runForm/runFormSlice";
import ModelSelector from "../components/ModelSelector/ModelSelector";
import type { SubjectType } from "../types";

export default function NewRun() {
  const nav = useNavigate();
  const dispatch = useAppDispatch();
  const form = useAppSelector((s) => s.runForm);
  const [createRun, { isLoading }] = useCreateRunMutation();

  const submit = async () => {
    const res = await createRun({
      subject_type: form.subject_type,
      subject: form.subject,
      task: form.task,
      model_config: form.model_config,
      plan_override: form.plan_override,
      uploaded_file_ids: form.uploaded_file_ids,
    }).unwrap();
    nav(`/runs/${res.run_id}`); // optimistic redirect + attach to stream
  };

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

          <TextField
            label="Task / instructions"
            value={form.task}
            onChange={(e) => dispatch(setTask(e.target.value))}
            multiline
            minRows={5}
          />

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
