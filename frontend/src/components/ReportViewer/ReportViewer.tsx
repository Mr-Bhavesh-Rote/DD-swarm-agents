// ReportViewer (§8.1): tabbed Final | Raw | Sources with sticky section nav and clickable
// [n] citations. Export dropdown for PDF/Word × Final/Raw.
import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  List,
  ListItemButton,
  ListItemText,
  Menu,
  MenuItem,
  Paper,
  Tab,
  Tabs,
  Typography,
  Link as MuiLink,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/Download";
import { useGetFinalQuery, useGetRawQuery, downloadExport } from "../../api/runsApi";
import { useAppDispatch, useAppSelector } from "../../app/store";
import { setTab, setActiveSection } from "../../features/viewer/viewerSlice";
import CitationMarkdown from "../CitationLink/CitationLink";
import type { Source } from "../../types";

export default function ReportViewer({ runId, canExport }: { runId: string; canExport: boolean }) {
  const dispatch = useAppDispatch();
  const tab = useAppSelector((s) => s.viewer.tab);
  const activeSection = useAppSelector((s) => s.viewer.activeSection);
  const { data: final } = useGetFinalQuery(runId);
  const { data: raw } = useGetRawQuery(runId);
  const [anchor, setAnchor] = useState<null | HTMLElement>(null);
  const [busy, setBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const sources: Source[] = (tab === "raw" ? raw?.sources : final?.sources) ?? [];

  return (
    <Box>
      <Box sx={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Tabs value={tab} onChange={(_e, v) => dispatch(setTab(v))}>
          <Tab label="Final" value="final" />
          <Tab label="Raw" value="raw" />
          <Tab label="Sources" value="sources" />
        </Tabs>
        <Box>
          <Button
            startIcon={<DownloadIcon />}
            variant="contained"
            disabled={!canExport || busy}
            onClick={(e) => setAnchor(e.currentTarget)}
          >
            {busy ? "Exporting…" : "Export"}
          </Button>
          <Menu anchorEl={anchor} open={!!anchor} onClose={() => setAnchor(null)}>
            {(["final", "raw"] as const).map((r) =>
              (["pdf", "docx"] as const).map((f) => (
                <MenuItem
                  key={`${r}-${f}`}
                  disabled={busy}
                  onClick={async () => {
                    setAnchor(null);
                    setBusy(true);
                    setExportError(null);
                    try {
                      await downloadExport(runId, f, r);
                    } catch (err: any) {
                      setExportError(err?.message ?? "Export failed");
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {r.toUpperCase()} → {f.toUpperCase()}
                </MenuItem>
              )),
            )}
          </Menu>
        </Box>
      </Box>
      {!canExport && (
        <Alert severity="info" sx={{ my: 1 }}>
          Export is available once the run status is <strong>done</strong> or <strong>needs review</strong>.
        </Alert>
      )}
      {exportError && (
        <Alert severity="error" sx={{ my: 1 }} onClose={() => setExportError(null)}>
          {exportError}
        </Alert>
      )}
      <Divider sx={{ my: 2 }} />

      {tab === "final" && final && (
        <Box sx={{ display: "flex", gap: 2 }}>
          <Paper variant="outlined" sx={{ position: "sticky", top: 16, alignSelf: "flex-start", minWidth: 220 }}>
            <List dense>
              {final.sections.map((sec) => (
                <ListItemButton
                  key={sec.id}
                  selected={activeSection === sec.id}
                  onClick={() => {
                    dispatch(setActiveSection(sec.id));
                    document.getElementById(`sec-${sec.id}`)?.scrollIntoView({ behavior: "smooth" });
                  }}
                >
                  <ListItemText primary={sec.title} />
                </ListItemButton>
              ))}
            </List>
          </Paper>
          <Box sx={{ flex: 1 }}>
            <Alert severity="success" sx={{ mb: 2 }}>
              Citation coverage {(final.verification.citation_coverage * 100).toFixed(0)}% · Faithfulness{" "}
              {(final.verification.faithfulness_score * 100).toFixed(0)}% · Flags{" "}
              {final.verification.flags.length}
            </Alert>
            {final.sections.map((sec) => (
              <Box key={sec.id} id={`sec-${sec.id}`} sx={{ mb: 3 }}>
                <Typography variant="h5" gutterBottom>
                  {sec.title}
                </Typography>
                <CitationMarkdown markdown={sec.body_markdown} sources={final.sources} />
              </Box>
            ))}
            {final.source_manifest && Object.keys(final.source_manifest).length > 0 && (
              <Box id="sec-sources-queried" sx={{ mb: 3 }}>
                <Typography variant="h5" gutterBottom>
                  Sources Queried
                </Typography>
                <List dense>
                  {Object.entries(final.source_manifest).map(([tool, info]) => (
                    <ListItemText
                      key={tool}
                      primary={
                        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                          <Chip size="small" color={info.attempted ? "success" : "warning"} label={info.attempted ? "queried" : "NOT queried"} />
                          <span>{tool}</span>
                        </Box>
                      }
                      secondary={`Required by: ${info.required_by}`}
                    />
                  ))}
                </List>
              </Box>
            )}
          </Box>
        </Box>
      )}

      {tab === "raw" && raw && (
        <Box>
          {raw.agent_outputs.map((ao) => (
            <Paper key={ao.agent} variant="outlined" sx={{ p: 2, mb: 2 }}>
              <Typography variant="h6">
                {ao.role || ao.agent} <Chip size="small" label={ao.model} />
              </Typography>
              <CitationMarkdown markdown={ao.narrative_markdown} sources={raw.sources} />
              {ao.findings.length > 0 && (
                <>
                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Findings
                  </Typography>
                  <List dense>
                    {ao.findings.map((f, i) => (
                      <ListItemText
                        key={i}
                        primary={f.claim}
                        secondary={`sources: ${(f.source_ids ?? f.source_urls ?? []).join(", ")} · ${f.confidence}`}
                      />
                    ))}
                  </List>
                </>
              )}
            </Paper>
          ))}
        </Box>
      )}

      {tab === "sources" && (
        <List>
          {sources.map((s) => (
            <ListItemText
              key={s.id}
              primary={
                <MuiLink href={s.url} target="_blank" rel="noopener noreferrer">
                  [{s.id}] {s.title || s.url}
                </MuiLink>
              }
              secondary={`${s.publisher} · ${s.retrieved_at ?? ""}`}
            />
          ))}
        </List>
      )}
    </Box>
  );
}
