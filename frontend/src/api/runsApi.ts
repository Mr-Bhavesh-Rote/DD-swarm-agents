// RTK Query API (§8.2). One service for runs, models, plan, reports, exports.
import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";
import type {
  FinalReport,
  ModelCatalogEntry,
  RawReport,
  RunListResponse,
  RunRequest,
  RunSummary,
  TaskRefineRequest,
  TaskRefineResponse,
  WorkflowPlan,
} from "../types";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

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
    refineTask: build.mutation<TaskRefineResponse, TaskRefineRequest>({
      query: (body) => ({ url: "/api/runs/refine-task", method: "POST", body }),
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
    approvePlan: build.mutation<{ run_id: string; status: string }, string>({
      query: (id) => ({ url: `/api/runs/${id}/approve-plan`, method: "POST" }),
      invalidatesTags: ["Run", "Plan"],
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

// Download an export with the auth header attached (a plain <a href> can't send the
// bearer token, which is why direct links returned 401). Fetches the file as a blob and
// triggers a save, keeping the JWT out of the URL/server logs.
export async function downloadExport(
  id: string,
  format: "pdf" | "docx",
  report: "raw" | "final",
): Promise<void> {
  const token = localStorage.getItem("token") ?? "";
  const resp = await fetch(exportUrl(id, format, report), {
    headers: { authorization: `Bearer ${token}` },
  });
  if (!resp.ok) {
    let detail = `Export failed (${resp.status})`;
    try {
      detail = (await resp.json())?.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  const blob = await resp.blob();
  const disposition = resp.headers.get("content-disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const filename = match?.[1] ?? `${report}.${format}`;
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}

export const streamUrl = (id: string) => `${API_BASE}/api/runs/${id}/stream`;

export const {
  useCreateRunMutation,
  useRefineTaskMutation,
  useApprovePlanMutation,
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
