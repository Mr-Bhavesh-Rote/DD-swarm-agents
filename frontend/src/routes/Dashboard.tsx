// Dashboard (§8.1): runs table with filters + New Run.
import { useState } from "react";
import {
  Box,
  Button,
  Chip,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { useNavigate } from "react-router-dom";
import { useGetRunsQuery } from "../api/runsApi";

export default function Dashboard() {
  const nav = useNavigate();
  const [subjectType, setSubjectType] = useState("");
  const [status, setStatus] = useState("");
  const { data } = useGetRunsQuery({
    subject_type: subjectType || undefined,
    status: status || undefined,
  });

  return (
    <Box>
      <Box sx={{ display: "flex", justifyContent: "space-between", mb: 2 }}>
        <Typography variant="h4">Runs</Typography>
        <Button variant="contained" onClick={() => nav("/runs/new")}>
          New Run
        </Button>
      </Box>

      <Box sx={{ display: "flex", gap: 2, mb: 2 }}>
        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel>Type</InputLabel>
          <Select label="Type" value={subjectType} onChange={(e) => setSubjectType(e.target.value)}>
            <MenuItem value="">All</MenuItem>
            <MenuItem value="company">Company</MenuItem>
            <MenuItem value="individual">Individual</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 160 }}>
          <InputLabel>Status</InputLabel>
          <Select label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
            {["", "queued", "planning", "researching", "synthesizing", "verifying", "done", "failed", "cancelled"].map(
              (s) => (
                <MenuItem key={s} value={s}>
                  {s || "All"}
                </MenuItem>
              ),
            )}
          </Select>
        </FormControl>
      </Box>

      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Subject</TableCell>
            <TableCell>Type</TableCell>
            <TableCell>Status</TableCell>
            <TableCell>Model</TableCell>
            <TableCell>Coverage</TableCell>
            <TableCell>Created</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {(data?.items ?? []).map((r) => (
            <TableRow key={r.id} hover sx={{ cursor: "pointer" }} onClick={() => nav(`/runs/${r.id}`)}>
              <TableCell>{r.subject}</TableCell>
              <TableCell>{r.subject_type}</TableCell>
              <TableCell>
                <Chip
                  size="small"
                  label={r.status}
                  color={r.status === "done" ? "success" : r.status === "failed" ? "error" : "info"}
                />
              </TableCell>
              <TableCell>{r.model ?? "—"}</TableCell>
              <TableCell>
                {r.citation_coverage != null ? `${(r.citation_coverage * 100).toFixed(0)}%` : "—"}
              </TableCell>
              <TableCell>{r.created_at?.slice(0, 16).replace("T", " ")}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Box>
  );
}
