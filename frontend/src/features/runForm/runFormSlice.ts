// runFormSlice (§8.2): subject_type, subject, task, model_config, plan_override.
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { ModelConfig, SubjectType, WorkflowPlan } from "../../types";

const COMPANY_TASK = `Produce a due-diligence report with these sections: Executive Summary (investment snapshot + recommendation), Ownership & Governance, Operations Footprint, Financial Performance, Risk Issues (Regulatory & Compliance, Legal & Litigation, Sanctions/AML/Corruption, Reputational & Media, ESG, Procurement/Counterparty, Jurisdictional, PEP/Political), and Investment Considerations. Cite every factual claim with [n] hyperlinked sources.`;

const INDIVIDUAL_TASK = `Produce a due-diligence profile with these sections: Identity & Background, Education & Career History, Current Role/Affiliations, Investment & Portfolio History, Net Worth (sourced/estimated), Board & Advisory Positions, Legal/Regulatory involvements, Controversies/Reputational, and Summary Assessment. Use public sources only; label estimates; cite every claim with [n] hyperlinked sources.`;

export const defaultTaskFor = (t: SubjectType) => (t === "company" ? COMPANY_TASK : INDIVIDUAL_TASK);

interface RunFormState {
  subject_type: SubjectType;
  subject: string;
  task: string;
  model_config: ModelConfig;
  plan_override: WorkflowPlan | null;
  uploaded_file_ids: string[];
}

const initialState: RunFormState = {
  subject_type: "company",
  subject: "",
  task: COMPANY_TASK,
  model_config: { global_default: "claude-opus-4-8", role_overrides: {} },
  plan_override: null,
  uploaded_file_ids: [],
};

const runFormSlice = createSlice({
  name: "runForm",
  initialState,
  reducers: {
    setSubjectType(state, action: PayloadAction<SubjectType>) {
      state.subject_type = action.payload;
      state.task = defaultTaskFor(action.payload);
    },
    setSubject(state, action: PayloadAction<string>) {
      state.subject = action.payload;
    },
    setTask(state, action: PayloadAction<string>) {
      state.task = action.payload;
    },
    setGlobalModel(state, action: PayloadAction<string>) {
      state.model_config.global_default = action.payload;
    },
    setRoleOverride(state, action: PayloadAction<{ role: string; model: string | null }>) {
      const { role, model } = action.payload;
      if (model) state.model_config.role_overrides[role] = model;
      else delete state.model_config.role_overrides[role];
    },
    setUploadedFileIds(state, action: PayloadAction<string[]>) {
      state.uploaded_file_ids = action.payload;
    },
    resetForm: () => initialState,
  },
});

export const {
  setSubjectType,
  setSubject,
  setTask,
  setGlobalModel,
  setRoleOverride,
  setUploadedFileIds,
  resetForm,
} = runFormSlice.actions;
export default runFormSlice.reducer;
