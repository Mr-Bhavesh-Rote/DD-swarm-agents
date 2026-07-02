// runFormSlice (§8.2): subject_type, subject, task, model_config, plan_override.
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { ModelConfig, PlanningMode, SubjectType, WorkflowPlan } from "../../types";

const COMPANY_TASK = `Produce a US-compliance adverse due-diligence report on this company. The report should be framed around DEROGATORY/ADVERSE risk material, not investment analysis.

Sections:
1. Executive Summary — lead with the most material adverse findings (sanctions, litigation, corruption, human-rights, controversial/dual-use products, environmental harm, etc.). No investment recommendation.
2. Subject Overview & Ownership — brief context only: what the company does, corporate structure, ultimate beneficial owners, key management, and any state/political ties or PEP exposure. No detailed financial statements or operational deep-dives.
3. Risk Issues — the core of the report. Cover ACTUAL issues affecting the subject, organized by: Sanctions/Export Controls/AML; Legal & Litigation; Corruption/Bribery/Fraud; Human Rights/Labor; Controversial/Dual-Use/Military Products & End-Use; Environmental/ESG Harm; Regulatory & Compliance Breaches; Reputational & Adverse Media; State Ownership/Political Ties/PEP; Jurisdictional & Counterparty Risk. For each issue include who, what, when, jurisdiction, and status where available.
4. Compliance Assessment & Confidence — overall risk rating (High/Medium/Low), confidence in sources, and information gaps. No investment recommendation.

Cite every factual claim with [n] hyperlinked sources. Use public sources only and label any estimate.`;

const INDIVIDUAL_TASK = `Produce a due-diligence profile with these sections: Identity & Background, Education & Career History, Current Role/Affiliations, Investment & Portfolio History, Net Worth (sourced/estimated), Board & Advisory Positions, Legal/Regulatory involvements, Controversies/Reputational, and Summary Assessment. Use public sources only; label estimates; cite every claim with [n] hyperlinked sources.`;

export const defaultTaskFor = (t: SubjectType) => (t === "company" ? COMPANY_TASK : INDIVIDUAL_TASK);

interface RunFormState {
  subject_type: SubjectType;
  subject: string;
  task: string;
  model_config: ModelConfig;
  plan_override: WorkflowPlan | null;
  uploaded_file_ids: string[];
  planning_mode: PlanningMode;
  max_research_agents: number | null;
}

const initialState: RunFormState = {
  subject_type: "company",
  subject: "",
  task: COMPANY_TASK,
  model_config: { global_default: "claude-sonnet-4-6", role_overrides: {} },
  plan_override: null,
  uploaded_file_ids: [],
  planning_mode: "template", // cheapest, bounded swarm — recommended default
  max_research_agents: 5, // matches backend default and new template swarm scale
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
    setPlanningMode(state, action: PayloadAction<PlanningMode>) {
      state.planning_mode = action.payload;
    },
    setMaxResearchAgents(state, action: PayloadAction<number | null>) {
      state.max_research_agents = action.payload;
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
  setPlanningMode,
  setMaxResearchAgents,
  resetForm,
} = runFormSlice.actions;
export default runFormSlice.reducer;
