// ModelSelector (§8.2): reads the server catalog, renders the global default + collapsible
// per-role overrides, writes into runFormSlice.model_config.
import { useState } from "react";
import {
  Box,
  Collapse,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Typography,
  Button,
  Chip,
} from "@mui/material";
import { useGetModelsQuery } from "../../api/runsApi";
import { useAppDispatch, useAppSelector } from "../../app/store";
import { setGlobalModel, setRoleOverride } from "../../features/runForm/runFormSlice";

const ROLES = ["orchestrator", "research", "aggregator", "writer", "verifier"];

export default function ModelSelector() {
  const dispatch = useAppDispatch();
  const { data: catalog = [] } = useGetModelsQuery();
  const modelConfig = useAppSelector((s) => s.runForm.model_config);
  const [showAdvanced, setShowAdvanced] = useState(false);

  return (
    <Box>
      <FormControl fullWidth size="small" sx={{ mb: 1 }}>
        <InputLabel>Global default model</InputLabel>
        <Select
          label="Global default model"
          value={modelConfig.global_default ?? ""}
          onChange={(e) => dispatch(setGlobalModel(e.target.value))}
        >
          {catalog.map((m) => (
            <MenuItem key={m.id} value={m.id}>
              {m.label} <Chip size="small" label={m.tier} sx={{ ml: 1 }} />
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      <Button size="small" onClick={() => setShowAdvanced((v) => !v)}>
        {showAdvanced ? "Hide" : "Show"} per-role overrides
      </Button>

      <Collapse in={showAdvanced}>
        <Box sx={{ mt: 1, display: "grid", gap: 1 }}>
          {ROLES.map((role) => (
            <FormControl key={role} size="small" fullWidth>
              <InputLabel>{role}</InputLabel>
              <Select
                label={role}
                value={modelConfig.role_overrides[role] ?? ""}
                onChange={(e) =>
                  dispatch(setRoleOverride({ role, model: e.target.value || null }))
                }
              >
                <MenuItem value="">
                  <em>(use global default)</em>
                </MenuItem>
                {catalog.map((m) => (
                  <MenuItem key={m.id} value={m.id}>
                    {m.label}
                    {m.recommended_roles.includes(role) ? " ★" : ""}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          ))}
          <Typography variant="caption" color="text.secondary">
            ★ = recommended for this role. Precedence: per-agent → per-role → global → system default.
          </Typography>
        </Box>
      </Collapse>
    </Box>
  );
}
