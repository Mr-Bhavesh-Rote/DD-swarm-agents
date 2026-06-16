import type { ReactNode } from "react";
import { AppBar, Box, Button, Container, Toolbar, Typography } from "@mui/material";
import { BrowserRouter, Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import Dashboard from "./routes/Dashboard";
import NewRun from "./routes/NewRun";
import RunDetail from "./routes/RunDetail";
import ReportViewerPage from "./routes/ReportViewerPage";
import Login from "./routes/Login";

function RequireAuth({ children }: { children: ReactNode }) {
  const token = localStorage.getItem("token");
  return token ? <>{children}</> : <Navigate to="/login" replace />;
}

function Shell({ children }: { children: ReactNode }) {
  const nav = useNavigate();
  const logout = () => {
    localStorage.removeItem("token");
    nav("/login");
  };
  return (
    <>
      <AppBar position="static">
        <Toolbar>
          <Typography variant="h6" sx={{ flexGrow: 1 }} component={Link} to="/" style={{ color: "white", textDecoration: "none" }}>
            Deep Due-Diligence
          </Typography>
          <Button color="inherit" onClick={logout}>
            Logout
          </Button>
        </Toolbar>
      </AppBar>
      <Container sx={{ py: 3 }}>{children}</Container>
    </>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/*"
          element={
            <RequireAuth>
              <Shell>
                <Box>
                  <Routes>
                    <Route path="/" element={<Dashboard />} />
                    <Route path="/runs/new" element={<NewRun />} />
                    <Route path="/runs/:id" element={<RunDetail />} />
                    <Route path="/runs/:id/report" element={<ReportViewerPage />} />
                  </Routes>
                </Box>
              </Shell>
            </RequireAuth>
          }
        />
      </Routes>
    </BrowserRouter>
  );
}
