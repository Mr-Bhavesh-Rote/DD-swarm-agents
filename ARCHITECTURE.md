# Deep-DD Pipeline Architecture

## Overview

Two planning modes produce the same downstream graph but differ in how the agent swarm is assembled:

| Mode | Trigger | Who builds the plan |
|------|---------|---------------------|
| **Template (Standard)** | `planning_mode = "template"` (default) | `config_loader` reads `config/agents.company.yaml` deterministically |
| **AI-Tailored** | `planning_mode = "ai"` | Orchestrator LLM generates a custom agent plan from the task description |

---

## Standard Flow (Template Mode)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  USER REQUEST                                                           │
│  subject: "Magnifica Air1 LLC"  subject_type: "company"                │
│  planning_mode: "template"                                              │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PLANNER NODE                                                           │
│  • Loads config/agents.company.yaml                                     │
│  • Parameterizes 5 fixed agent specs:                                   │
│      overview_ownership_researcher   (domain: overview_ownership)       │
│      sanctions_legal_researcher      (domain: sanctions_legal)          │
│      adverse_conduct_researcher      (domain: adverse_conduct)          │
│      adverse_media_esg_researcher    (domain: adverse_media_esg)        │
│      pep_ownership_risk_researcher   (domain: pep_ownership_risk)       │
│  • Returns normalized WorkflowPlan → state.plan                         │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      │  dispatch_overview()
                      │  fans out ONLY domain=overview_ownership agents
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PHASE 1 — OVERVIEW AGENT  (sequential, runs alone)                    │
│                                                                         │
│  overview_ownership_researcher                                          │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Tool-calling loop  (max_iterations: 10)                         │  │
│  │  • web_search: "[subject] major shareholders ownership"          │  │
│  │  • web_search: "[subject] corporate structure UBO"               │  │
│  │  • web_search: "[founder name] CIG Companies" ← chain-search    │  │
│  │  • scrape_url: relevant pages                                    │  │
│  │  Acceptance gate: ≥3 total calls + ≥1 web_search + findings>0   │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  Output → state.raw_outputs[], state.findings[], state.sources_raw[]   │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  ENTITY EXTRACTOR NODE                                                  │
│  • Reads raw_outputs where domain=overview_ownership                    │
│  • Collects up to 40 finding claims as plain text                       │
│  • Writes to state.overview_context                                     │
│                                                                         │
│  Example overview_context:                                              │
│    - Charles Carey is founder and CEO of Magnifica Air1 LLC             │
│    - CIG Companies LLC is the parent company (100% owner)               │
│    - Jeff Sheehan is UBO via Sheehan Partners & Trust                   │
│    - N321LC FAA N-Number reserved under "MAGNIFICA AIR LLC"             │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      │  dispatch_adverse()
                      │  fans out all non-overview agents
                      │  injects overview_context into each agent's prompt
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PHASE 2 — ADVERSE AGENTS  (parallel, up to MAX_SUBAGENTS cap)         │
│                                                                         │
│  Each agent receives:                                                   │
│    (1) its own domain goal from agents.company.yaml                     │
│    (2) overview_context → KEY ENTITIES block in tool instructions       │
│    (3) CHAIN-SEARCH RULE: follow up on every entity discovered          │
│                                                                         │
│  ┌──────────────────────┐  ┌──────────────────────┐                    │
│  │ sanctions_legal      │  │ adverse_conduct       │                    │
│  │ max_iterations: 10   │  │ max_iterations: 10    │                    │
│  │                      │  │                       │                    │
│  │ Sees entity context: │  │ Sees entity context:  │                    │
│  │ "CIG Companies LLC   │  │ "CIG Companies LLC    │                    │
│  │  is parent company"  │  │  is parent company"   │                    │
│  │                      │  │                       │                    │
│  │ Searches:            │  │ Searches:             │                    │
│  │ • "CIG Companies     │  │ • "CIG Companies      │                    │
│  │    lawsuit court"  ──┼──┼▶  fraud corruption"   │                    │
│  │ • OFAC/BIS/UN/EU    │  │ • FPDS/OCCRP          │                    │
│  │ • PACER             │  │ • "Charles Carey       │                    │
│  │                      │  │    Atlanta hotel" ────┼──▶ finds OBJ article│
│  └──────────────────────┘  └──────────────────────┘                    │
│                                                                         │
│  ┌──────────────────────┐  ┌──────────────────────┐                    │
│  │ adverse_media_esg    │  │ pep_ownership_risk    │                    │
│  │ max_iterations: 10   │  │ max_iterations: 10    │                    │
│  │ EPA ECHO/OSHA/       │  │ OFAC/PACER/           │                    │
│  │ Violation Tracker    │  │ Who Profits           │                    │
│  └──────────────────────┘  └──────────────────────┘                    │
│                                                                         │
│  Tool-calling acceptance gate (per agent):                              │
│    ✓ ≥3 total tool calls                                                │
│    ✓ ≥1 web_search (compliance-DB-only calls rejected)                 │
│    ✓ findings > 0  OR  pushed back ≥2 times (genuinely clean subject)  │
│                                                                         │
│  Forced search fallback (when gate not met):                            │
│    Domain-specific queries auto-executed in code if LLM won't cooperate │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │  all parallel branches complete → state merged
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  AGGREGATOR NODE                                                        │
│  • Builds global citation registry (dedupe by canonical URL, assign [n])│
│  • Resolves finding.source_urls → source_ids                           │
│  • Deduplicates findings by normalized claim text                       │
│  • LLM-driven bucketing: assigns category + severity to each finding    │
│  • Fact classification: core / analysis / unverified / advocacy         │
│  • Circular dependency detection                                        │
│  • Per-finding confidence scoring (source tier × circular-dep penalty)  │
│  Output → state.aggregated_findings[], state.sources[], state.buckets[] │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  SYNTHESIZER NODE                                                       │
│  • Drafts each report section from aggregated_findings                  │
│  • Post-processing pipeline (in order):                                 │
│      A1. _inject_missing_citations()  — adds [n] to [REPORTED] bullets  │
│      A2. _strip_hallucinated_bullets() — removes claims not in corpus   │
│      B.  Pre-verify coverage check — if section < 80% cited, redraft   │
│  • Sections: Executive Summary, Risk Issues, PEP Status,               │
│              Ownership & Structure, Adverse Media                       │
│  • PEP fallback: _fallback_pep_status() if LLM returns blank           │
│  Output → state.draft_sections[]                                        │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  VERIFIER NODE                                                          │
│  • Batches each claim against retrieved source text                     │
│  • Scores: SUPPORTED / PARTIALLY / UNSUPPORTED / UNVERIFIABLE          │
│  • Computes faithfulness_score = supported / (supported + unsupported)  │
│  • Computes citation_coverage = claims_with_citation / total_claims     │
│  • Skips LLM check if < 50% sources have retrievable text              │
│  Output → state.verification{}                                          │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      │  route_after_verify()
                      │  faithfulness < 0.85 AND revision_count < max_revisions?
                 ┌────┴────┐
                 │         │
             YES ▼     NO  ▼
         synthesizer   quality_gate
         (revise loop)
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  QUALITY GATE NODE                                                      │
│  Phase 1: Source quality  — tier classification (primary/secondary/     │
│           tertiary), retrievability check                               │
│  Phase 2: Finding classification — fact / analysis / interpretation /   │
│           advocacy                                                      │
│  Phase 3: Circular dependency detection — flags self-referencing claims │
│  Phase 4: Automated pass/fail gates (4 checks):                        │
│           • faithfulness ≥ threshold                                    │
│           • citation coverage ≥ threshold                               │
│           • minimum findings count                                      │
│           • source diversity                                            │
│  Output → state.quality_assessment{}                                    │
│  Run status: "done" | "needs_review" (if any gate fails)               │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  RENDERER NODE                                                          │
│  • Assembles raw_report (all sections + verification metadata)          │
│  • Assembles final_report (cleaned, citation-complete)                  │
│  • render_markdown() → human-readable .md / .docx export               │
│  Output → state.raw_report{}, state.final_report{}                     │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
                    END
          ↓ persist to Postgres
          RunAgent rows, SourceRow rows, FindingRow rows, Report rows
```

---

## AI-Tailored Flow

Identical downstream graph — only the **Planner** behaves differently.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  USER REQUEST                                                           │
│  subject: "JSC KazMunayGas"  subject_type: "company"                   │
│  planning_mode: "ai"                                                    │
│  task: "Focus on OFAC secondary sanctions exposure and Kazakhgate"      │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PLANNER NODE  (AI mode)                                                │
│                                                                         │
│  • Skips config/agents.company.yaml                                     │
│  • Calls orchestrator LLM (claude-sonnet-4-6) with:                    │
│      - subject, subject_type, task description                          │
│      - max_agents cap (min of max_research_agents, MAX_SUBAGENTS)       │
│  • LLM generates a custom WorkflowPlan:                                 │
│      e.g. adds "kazakhgate_researcher" with OCCRP-heavy toolset         │
│           adds "secondary_sanctions_researcher" targeting Russian ties   │
│           may skip adverse_media_esg if task doesn't require it         │
│  • _ensure_domain_coverage() injects default agents for any of the 5   │
│    required domains still missing from the generated plan               │
│  Output → state.plan (is_generated=True)                                │
└─────────────────────┬───────────────────────────────────────────────────┘
                      │
                      │  Same two-phase dispatch as Standard flow
                      │  (dispatch_overview → entity_extractor → dispatch_adverse)
                      ▼
              [identical to Standard flow from Phase 1 onward]
```

---

## Key Differences: Standard vs AI-Tailored

| Aspect | Standard (Template) | AI-Tailored |
|--------|-------------------|-------------|
| Agent count | Fixed 5 (from YAML) | LLM decides (1–MAX_SUBAGENTS) |
| Agent names | Fixed canonical names | LLM-generated, task-specific |
| Domain coverage | Always all 5 domains | LLM may focus on subset; `_ensure_domain_coverage` fills gaps |
| Agent goals | From `agents.company.yaml` | LLM-written, task-specific |
| Tool selection | From `agents.company.yaml` | LLM-selected per agent |
| Max iterations | From `agents.company.yaml` | LLM-specified (fallback defaults if missing) |
| Cost | Predictable | Higher (LLM planning step) |
| Reproducibility | Deterministic agent spec | Varies per LLM call |

---

## Two-Phase Dispatch Detail

```
dispatch_overview()
  │
  ├─ domain == "overview_ownership"?  YES → Send("overview_agent", payload)
  └─ No overview agents in plan?      YES → route directly to "entity_extractor"

entity_extractor_node()
  │  reads raw_outputs where domain="overview_ownership"
  │  collects finding.claim strings (up to 40)
  └─ writes overview_context to state

dispatch_adverse()
  │
  ├─ skip domain == "overview_ownership" (already ran)
  ├─ for each remaining agent: Send("research_agent", {overview_context, ...})
  └─ cap at MAX_SUBAGENTS
```

---

## Research Agent Tool-Calling Loop

```
For each research agent (overview or adverse):

  START
    │
    ├─ [max_iterations] main loop
    │     LLM response?
    │     ├─ tool call JSON      → execute tool, append TOOL_RESULT, continue
    │     ├─ final answer        → check acceptance gate:
    │     │     ✓ total calls ≥ 3
    │     │     ✓ web_search calls ≥ 1       (compliance-DB-only rejected)
    │     │     ✓ findings > 0  OR  zero_findings_rejects ≥ 2
    │     │     → ACCEPT: _finalize()
    │     │     → REJECT: targeted pushback message, continue
    │     └─ unparseable         → "Respond ONLY with JSON", continue
    │
    ├─ [post-loop] required compliance tools gate
    │     missing = REQUIRED_TOOLS_BY_DOMAIN[domain] - called_tools
    │     up to 2 extra turns forcing missing compliance DB calls
    │     acceptance here requires ≥1 web_search before accepting
    │
    └─ [post-loop] forced search fallback
          triggers when: total_calls < 3  OR  web_search_calls == 0
          auto-executes domain-specific queries:
            sanctions_legal  → lawsuit/enforcement/court queries
            adverse_conduct  → corruption/FCPA/human-rights queries
            adverse_media_esg → environmental/workplace/scandal queries
            overview         → shareholders/controversy queries
          3-turn LLM processing loop to extract findings from results
          final forced synthesis if still no answer
```

---

## State Schema (reducer channels)

```
GraphState
  ├─ inputs (set once):      run_id, subject, subject_type, task, model_config,
  │                          plan_override, uploaded_file_ids, planning_mode
  │
  ├─ planner output:         plan{}
  │
  ├─ entity extractor:       overview_context  (str — injected into adverse prompts)
  │
  ├─ research swarm          raw_outputs[]     ← operator.add  (append-only)
  │  (reducer-merged):       findings[]        ← operator.add
  │                          sources_raw[]     ← operator.add
  │
  ├─ aggregator output:      aggregated_findings[], sources[], buckets[]
  │
  ├─ synthesizer/verifier:   draft_sections[], verification{}, revision_count,
  │                          needs_revision
  │
  ├─ quality gate:           quality_assessment{}
  │
  ├─ final artifacts:        raw_report{}, final_report{}
  │
  └─ bookkeeping:            model_summary{}   ← _merge_dicts
                             cost_usd          ← operator.add
                             events[]          ← operator.add
```

---

## Compliance Tool Registry

| Tool | Domain | Database |
|------|--------|----------|
| `ofac_sdn_search` | sanctions_legal, pep_ownership_risk | OFAC SDN List |
| `ofac_nonsdn_search` | sanctions_legal | OFAC Non-SDN Lists |
| `bis_entity_list_search` | sanctions_legal | BIS Entity List |
| `un_sanctions_search` | sanctions_legal | UN Sanctions List |
| `eu_sanctions_search` | sanctions_legal | EU Sanctions List |
| `pacer_search` | sanctions_legal, pep_ownership_risk | PACER (US federal courts) |
| `fpds_search` | adverse_conduct | FPDS (federal procurement) |
| `usaspending_search` | adverse_conduct | USASpending.gov |
| `occrp_search` | adverse_conduct | OCCRP ALEPH |
| `who_profits_search` | adverse_conduct, pep_ownership_risk | Who Profits database |
| `epa_echo_search` | adverse_media_esg | EPA ECHO |
| `osha_search` | adverse_media_esg | OSHA enforcement |
| `violation_tracker_search` | adverse_media_esg | Violation Tracker |
| `web_search` | all | Tavily (advanced depth, 8 results) |
| `scrape_url` | all | HTTP scraper (50k char cap) |

---

## Run Status Flow

```
queued → planning → researching → synthesizing → verifying → done
                                                           ↘ needs_review  (quality gate fail)
                                                           ↘ failed        (unhandled error)
                                                           ↘ cancelled     (user cancelled)
```
