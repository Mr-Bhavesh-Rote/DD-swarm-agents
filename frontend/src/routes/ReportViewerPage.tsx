import { useState } from "react";
import { Alert, Box, Button, Snackbar, Stack, Typography } from "@mui/material";
import { useParams } from "react-router-dom";
import { useGetRunQuery, useMarkReviewedMutation } from "../api/runsApi";
import ReportViewer from "../components/ReportViewer/ReportViewer";

export default function ReportViewerPage() {
  const { id } = useParams<{ id: string }>();
  const { data: run } = useGetRunQuery(id!);
  const [markReviewed, { isLoading: marking }] = useMarkReviewedMutation();
  const [error, setError] = useState<string | null>(null);
  const isTerminal = run?.status === "done" || run?.status === "needs_review";
  const needsReview = run?.status === "needs_review";

  const onMarkReviewed = async () => {
    if (!id) return;
    setError(null);
    try {
      await markReviewed(id).unwrap();
    } catch (err: any) {
      setError(err?.data?.detail || err?.message || "Failed to mark reviewed");
    }
  };

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h4">{run?.subject}</Typography>
        {isTerminal && !run?.reviewed && (
          <Button variant="outlined" disabled={marking} onClick={onMarkReviewed}>
            {marking ? "Marking reviewed…" : "Mark reviewed"}
          </Button>
        )}
        {run?.reviewed && <Typography color="success.main">✓ Reviewed</Typography>}
      </Stack>
      {needsReview && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          This report scored below the faithfulness threshold and is flagged for manual review.
          Clicking <strong>Mark reviewed</strong> confirms the findings are acceptable and moves the run to <strong>done</strong>.
        </Alert>
      )}
      {id && <ReportViewer runId={id} canExport={isTerminal} />}
      <Snackbar
        open={!!error}
        autoHideDuration={6000}
        onClose={() => setError(null)}
        message={error}
      />
    </Box>
  );
}
