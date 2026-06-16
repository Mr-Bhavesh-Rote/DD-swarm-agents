// RTK Query API (§8.2). One service for runs, models, plan, reports, exports.
import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";
import type {
  FinalReport,
  ModelCatalogEntry,
  RawReport,
  RunListResponse,
  RunRequest,
  RunSummary,
  WorkflowPlan,
} from "../types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export const runsApi = createApi({
  reducerPath: "runsApi",
  baseQuery: fetchBaseQuery({
    baseUrl: API_BASE,
    prepareHeaders: (headers) => {
      const token = localStorage.getItem("token");
      if (token) headers.set("authorization", `Bearer ${token}`);
      return headers;
    },
  }),
  tagTypes: ["Run", "Plan"],
  endpoints: (build) => ({
    createRun: build.mutation<{ run_id: string; status: string }, RunRequest>({
      query: (body) => ({ url: "/api/runs", method: "POST", body }),
      invalidatesTags: ["Run"],
    }),
    getRuns: build.query<RunListResponse, { subject_type?: string; status?: string; page?: number }>({
      query: (params) => ({ url: "/api/runs", params }),
      providesTags: ["Run"],
    }),
    getRun: build.query<RunSummary, string>({
      query: (id) => `/api/runs/${id}`,
      providesTags: ["Run"],
    }),
    getPlan: build.query<WorkflowPlan, string>({
      query: (id) => `/api/runs/${id}/plan`,
      providesTags: ["Plan"],
    }),
    updatePlan: build.mutation<WorkflowPlan, { id: string; plan: WorkflowPlan }>({
      query: ({ id, plan }) => ({ url: `/api/runs/${id}/plan`, method: "PUT", body: plan }),
      invalidatesTags: ["Plan"],
    }),
    cancelRun: build.mutation<{ run_id: string; status: string }, string>({
      query: (id) => ({ url: `/api/runs/${id}/cancel`, method: "POST" }),
      invalidatesTags: ["Run"],
    }),
    resumeRun: build.mutation<{ run_id: string; status: string }, string>({
      query: (id) => ({ url: `/api/runs/${id}/resume`, method: "POST" }),
      invalidatesTags: ["Run"],
    }),
    markReviewed: build.mutation<{ run_id: string; reviewed: boolean }, string>({
      query: (id) => ({ url: `/api/runs/${id}/review`, method: "POST" }),
      invalidatesTags: ["Run"],
    }),
    getRaw: build.query<RawReport, string>({ query: (id) => `/api/runs/${id}/raw` }),
    getFinal: build.query<FinalReport, string>({ query: (id) => `/api/runs/${id}/final` }),
    getModels: build.query<ModelCatalogEntry[], void>({ query: () => "/api/models" }),
    getTrace: build.query<{ trace_url: string }, string>({ query: (id) => `/api/runs/${id}/trace` }),
  }),
});

export const exportUrl = (id: string, format: "pdf" | "docx", report: "raw" | "final") =>
  `${API_BASE}/api/runs/${id}/export?format=${format}&report=${report}`;

export const streamUrl = (id: string) => `${API_BASE}/api/runs/${id}/stream`;

export const {
  useCreateRunMutation,
  useGetRunsQuery,
  useGetRunQuery,
  useGetPlanQuery,
  useUpdatePlanMutation,
  useCancelRunMutation,
  useResumeRunMutation,
  useMarkReviewedMutation,
  useGetRawQuery,
  useGetFinalQuery,
  useGetModelsQuery,
  useGetTraceQuery,
} = runsApi;
