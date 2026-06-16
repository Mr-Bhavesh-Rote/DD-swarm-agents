import { Box, Button, Stack, Typography } from "@mui/material";
import { useParams } from "react-router-dom";
import { useGetRunQuery, useMarkReviewedMutation } from "../api/runsApi";
import ReportViewer from "../components/ReportViewer/ReportViewer";

export default function ReportViewerPage() {
  const { id } = useParams<{ id: string }>();
  const { data: run } = useGetRunQuery(id!);
  const [markReviewed] = useMarkReviewedMutation();
  const canExport = run?.status === "done";

  return (
    <Box>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h4">{run?.subject}</Typography>
        {canExport && !run?.reviewed && (
          <Button variant="outlined" onClick={() => id && markReviewed(id)}>
            Mark reviewed
          </Button>
        )}
        {run?.reviewed && <Typography color="success.main">✓ Reviewed</Typography>}
      </Stack>
      {id && <ReportViewer runId={id} canExport={!!canExport} />}
    </Box>
  );
}
