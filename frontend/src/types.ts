// Shared TypeScript contracts mirroring the backend JSON schemas (§5).

export type SubjectType = "company" | "individual";
export type Confidence = "high" | "medium" | "low";

export interface ModelConfig {
  global_default?: string;
  role_overrides: Record<string, string>;
}

export interface ModelCatalogEntry {
  id: string;
  label: string;
  tier: string;
  recommended_roles: string[];
}

export interface AgentSpec {
  name: string;
  role: string;
  goal: string;
  rationale?: string;
  depends_on: string[];
  max_iterations: number;
  suggested_tools: string[];
  model?: string | null;
  provider: string;
}

export interface WorkflowPlan {
  task: string;
  summary: string;
  execution_notes: string;
  agents: AgentSpec[];
}

export interface RunRequest {
  subject_type: SubjectType;
  subject: string;
  task: string;
  model_config: ModelConfig;
  plan_override?: WorkflowPlan | null;
  uploaded_file_ids: string[];
}

export interface Source {
  id: number;
  url: string;
  title: string;
  publisher: string;
  retrieved_at?: string;
  snippet: string;
  content_hash: string;
}

export interface Finding {
  agent: string;
  claim: string;
  source_ids: number[];
  source_urls?: string[];
  confidence: Confidence;
  category?: string | null;
}

export interface ToolCall {
  tool: string;
  input: Record<string, unknown>;
  output_summary: string;
}

export interface AgentOutput {
  agent: string;
  role: string;
  model: string;
  narrative_markdown: string;
  findings: Finding[];
  tool_calls: ToolCall[];
}

export interface RawReport {
  run_id: string;
  subject: string;
  subject_type: SubjectType;
  generated_at: string;
  agent_outputs: AgentOutput[];
  sources: Source[];
}

export interface SectionTable {
  title: string;
  columns: string[];
  rows: string[][];
}

export interface ReportSection {
  id: string;
  title: string;
  body_markdown: string;
  tables: SectionTable[];
  citations: number[];
}

export interface Verification {
  citation_coverage: number;
  faithfulness_score: number;
  flags: Array<{
    section_id: string;
    claim: string;
    citation_ids: number[];
    reason: string;
    status: string;
  }>;
}

export interface FinalReport {
  run_id: string;
  subject: string;
  subject_type: SubjectType;
  generated_at: string;
  model_summary: Record<string, string>;
  verification: Verification;
  sections: ReportSection[];
  sources: Source[];
}

export interface RunSummary {
  id: string;
  subject: string;
  subject_type: SubjectType;
  status: string;
  model?: string;
  cost_usd: number;
  reviewed: boolean;
  citation_coverage?: number | null;
  created_at?: string;
  finished_at?: string;
  langfuse_trace_id?: string;
  error?: string;
  verification?: Verification;
  model_config?: ModelConfig;
  alive?: boolean;
  heartbeat_age?: number | null;
}

export interface RunListResponse {
  items: RunSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface ResearchAgentInfo {
  name: string;
  role: string;
  model: string;
}

export interface ProgressEvent {
  node: string;
  agent?: string;
  status?: string;
  model?: string;
  n_findings?: number;
  n_tool_calls?: number;
  citation_coverage?: number;
  faithfulness_score?: number;
  n_flags?: number;
  needs_revision?: boolean;
  research_agents?: ResearchAgentInfo[];
  error?: string;
  run_id: string;
}

// One research-agent card's live state in the swarm view.
export interface AgentCard {
  agent: string;
  role?: string;
  model?: string;
  status: "pending" | "running" | "completed" | string;
  n_findings?: number;
  n_tool_calls?: number;
}
