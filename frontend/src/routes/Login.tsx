import { useState } from "react";
import { Box, Button, Card, CardContent, TextField, Typography, Alert, Tabs, Tab } from "@mui/material";
import { useNavigate } from "react-router-dom";
import { useLoginMutation, useRegisterMutation } from "../api/authApi";

export default function Login() {
  const nav = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [login] = useLoginMutation();
  const [register] = useRegisterMutation();

  const submit = async () => {
    setError("");
    try {
      const fn = mode === "login" ? login : register;
      const res = await fn({ email, password }).unwrap();
      localStorage.setItem("token", res.access_token);
      localStorage.setItem("role", res.role);
      nav("/");
    } catch (e: any) {
      setError(e?.data?.detail ?? "Authentication failed");
    }
  };

  return (
    <Box sx={{ display: "flex", justifyContent: "center", mt: 8 }}>
      <Card sx={{ width: 380 }}>
        <CardContent>
          <Typography variant="h5" gutterBottom>
            Deep Due-Diligence
          </Typography>
          <Tabs value={mode} onChange={(_e, v) => setMode(v)} sx={{ mb: 2 }}>
            <Tab label="Login" value="login" />
            <Tab label="Register" value="register" />
          </Tabs>
          {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
          <TextField fullWidth label="Email" value={email} onChange={(e) => setEmail(e.target.value)} sx={{ mb: 2 }} />
          <TextField
            fullWidth
            type="password"
            label="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            sx={{ mb: 2 }}
          />
          <Button fullWidth variant="contained" onClick={submit}>
            {mode === "login" ? "Login" : "Register"}
          </Button>
        </CardContent>
      </Card>
    </Box>
  );
}
