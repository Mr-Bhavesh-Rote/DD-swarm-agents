// Auth API (login/register). Tokens are stored in localStorage and attached by runsApi.
import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

interface TokenResponse {
  access_token: string;
  token_type: string;
  role: string;
  email: string;
}

export const authApi = createApi({
  reducerPath: "authApi",
  baseQuery: fetchBaseQuery({ baseUrl: API_BASE }),
  endpoints: (build) => ({
    login: build.mutation<TokenResponse, { email: string; password: string }>({
      query: (body) => ({ url: "/api/auth/login", method: "POST", body }),
    }),
    register: build.mutation<TokenResponse, { email: string; password: string; role?: string }>({
      query: (body) => ({ url: "/api/auth/register", method: "POST", body }),
    }),
  }),
});

export const { useLoginMutation, useRegisterMutation } = authApi;
