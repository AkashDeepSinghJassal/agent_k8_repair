# Prototype v4 contract: hybrid with progressive crystallization

Branch `prototype-v4` (from `prototype-v3`). All subagents build against this file; if you must deviate, edit this file in the same change and say so in your report. `AGENT_CONTRACT.md` (v3) still holds for the agent, the MCP servers and the operator integration; this file adds to it.

Background reading: `research/progressive-crystallization-overview.md` (three execution types, promotion and demotion), `research/cloud-opsbench-overview.md` (evidence graphs, outcome vs process), `research/hyperscale-incident-resolution-overview.md` (rollback, circuit breakers, observability).

## Idea in one paragraph

Every failure starts as a **T3 agent run** (the v3 MCP agent, Claude CLI by default). When an incident ends **Healed** and the merged change equals the proposal, the operator **crystallizes** the agent's trace into a **playbook**, keyed by the knowledge signature: the parameterized tool sequence it used, and the edit with each value's **provenance** (which tool output it came from and how to extract it). The next time the same signature fails, the **router** picks the cheapest tier the playbook has earned: **T1 deterministic** (replay the tool calls, extract the values, render the edit; no LLM), **T2 hybrid** (replay the tool calls, then one narrow LLM call without tools to fill the edit), or **T3 agent**. Playbooks are **promoted** T3 → T2 → T1 on evidence and **demoted** on failure. Every step, from detection to learning, is written to a **flow trace** that the dashboard shows in a trace pop-up.

## 1. Flow trace (module `repair_operator/flow.py`)

One JSON Lines file per incident: `<state_dir>/flows/<namespace>__<incident>.jsonl`. Append-only, one event per line. Must never raise into the caller (log and continue).

```python
def emit(incident: str, namespace: str, kind: str, *, title: str, data: dict | None = None,
         tier: str | None = None, parent: str | None = None, status: str = "ok") -> str:
    """Append one event; returns its id (uuid4 hex[:12])."""
def read(incident: str, namespace: str) -> list[dict]: ...
def span(incident, namespace, kind, *, title, data=None, tier=None, parent=None):  # context manager
    """emits kind with status 'running' and data, then on exit an event with the same id and
    status 'ok' | 'error', duration_ms, and any data added via the yielded dict."""
```

Event fields: `id`, `ts` (ISO-8601 UTC with ms), `incident`, `namespace`, `kind`, `title` (one human sentence), `tier` (`T1`|`T2`|`T3`|null), `parent` (event id or null), `status` (`ok`|`error`|`running`|`skipped`), `duration_ms` (optional), `data` (dict; large text values capped at 20,000 characters each, with `"…[truncated N chars]"`).

`kind` vocabulary (the UI groups and colours by kind; add new kinds only here):

| kind | emitted by | data keys (minimum) |
| --- | --- | --- |
| `detected` | incident created | source (kind, name, owner, container, reason, classHint), fingerprint |
| `evidence` | collect step | files, events_count, log_lines, configmaps |
| `route` | router | signature, playbook_id, playbook_state, chosen tier, reason (e.g. "promoted to T1 after 3 verified runs"), fallback_from |
| `agent_start` / `agent_end` | T3 | provider, model, seed / status, steps, input_tokens, output_tokens, cost_usd, latency_ms, fallback_from |
| `tool_call` | T3 agent (each MCP call), T2/T1 replay | server, tool, args (full), result (full text, capped), ms, error, step |
| `llm_call` | T2 narrow call (and v2 one-shot if used) | provider, model, prompt (capped), response (capped), tokens, ms |
| `extract` | T1/T2 | variable, from_step, rule, value or error |
| `proposal` | analyzer | tier, class, root_cause, confidence, file, edits, rationale |
| `guardrail` | analyze_step | one event per check: name, ok, detail |
| `pr` | propose_step | number, url, branch |
| `review` | review poll | state (merged / changes_requested / closed), feedback |
| `sync` / `verify` | verify handlers | revision, synced / healthySince, verdict, details |
| `outcome` | terminal phase | phase (Healed / Failed / Escalated / Dismissed), reason |
| `learn` | learner | action (`captured` / `observed` / `promoted` / `demoted` / `skipped`), playbook_id, from_state, to_state, counts, reason |

`tool_call` events from the agent hosts: the hosts get an optional callback `on_tool(event_dict)`; `runner.investigate(..., on_event=callable)` passes it through; the operator adapter wires it to `flow.emit(..., kind="tool_call", tier="T3", parent=<agent_start id>)`. The Claude host emits when it parses each `tool_result`; the Groq host after each call. **Trace entries in `AgentResult.trace` gain `result` (full text, capped 20,000 chars) next to `result_preview`.**

Dashboard API: `GET /api/incidents/{ns}/{name}/flow` → `{"incident", "namespace", "events": [...]}` ordered by ts; `GET /api/flows/recent?limit=20` → latest incidents with summary (tier used, outcome, steps, tokens).

## 2. Playbooks (module `repair_operator/playbooks.py`, SQLite at `<state_dir>/playbooks.db`)

```python
@dataclass
class Step:            # one replayable tool call
    server: str; tool: str
    args: dict         # values may contain ${var} placeholders bound from the incident seed
                       # (namespace, pod, container, owner, deployment, file, repo, app) or from earlier extracts
@dataclass
class Extract:         # how to get one variable from a step's output
    var: str; from_step: int
    rule: dict         # {"type": "regex", "pattern": "...", "group": 1}  or  {"type": "json_path", "path": "a.b[0]"}
                       # or {"type": "diff_removed_line", "key": "DB_URL"}  (value of a key on a '-' line of a git diff)
@dataclass
class Playbook:
    id: str; signature: str; cls: str
    state: str                  # "T3" (candidate) | "T2" | "T1"
    steps: list[Step]; extracts: list[Extract]
    file_template: str          # e.g. "deploy/${ns_dir}/configmaps.yaml" or a literal path
    edits_template: list[dict]  # [{"find": "...${var}...", "replace": "...${var}..."}]
    action_sequence: list[str]  # tool names in order, for consistency checks
    runs: dict                  # {"t3_ok", "t2_ok", "t1_ok", "fail", "edited"}
    consistency: float          # share of T3 runs with the same action sequence
    history: list[dict]         # [{ts, event, from, to, reason, incident}]
    created: str; updated: str
```

Store API: `get_by_signature(sig)`, `upsert(pb)`, `list_all()`, `record(pb_id, event, incident, ok, reason)`.

**Capture** (`crystallize.capture(trace, proposal, seed) -> Playbook | None`): from a T3 trace whose incident Healed with `mergedMatchesProposal == True`:
- keep only successful read tool calls that contributed (drop calls whose result was an error; drop exact repeats); parameterize literal values that equal seed fields (namespace, pod name → `${pod}` but prefer owner/deployment-based args since pod names change; container, file, repo);
- for every literal in the proposal's `edits[].replace` that is not in `edits[].find` (the new values), find the earliest tool result containing it and derive an `Extract` rule (prefer `diff_removed_line` when it comes from `git_diff`; else a regex anchored on the surrounding key, e.g. `DB_URL:\s*(\S+)`); if any new literal has no provenance, the playbook can reach T2 but **never T1** (record `t1_blocked: "value X has no provenance"`);
- the `find` side is templated with the same variables when its text came from a `git_read` of the file.

**Promotion** (defaults; env-configurable for demos: `REPAIR_PROMOTE_T2_AFTER`, `REPAIR_PROMOTE_T1_AFTER`, default 2 and 2 for the demo; the thesis default is 10/50 per the paper, documented):
- T3 → T2: `t3_ok >= PROMOTE_T2_AFTER` verified heals for the signature, `consistency >= 0.9`, zero guardrail rejections and zero reviewer edits in those runs.
- T2 → T1: `t2_ok >= PROMOTE_T1_AFTER` verified hybrid heals where the LLM-filled values equalled the extract-rule values (the rule would have produced the same edit), and no `t1_blocked`.
- **Demotion** (circuit breaker): a failed verification, a guardrail rejection, an extraction failure or a reviewer edit on a T1/T2 run demotes one level (T1 → T2 → T3) and records why. The incident itself falls back to the next tier up in the same attempt (T1 fails to extract → try T2 → try T3), emitting a `route` event with `fallback_from`.

## 3. Tier executors (package `repair_operator/tiers/`)

- `t1.py: run(playbook, seed, mcp) -> TierResult`: replay `steps` through the same MCP servers (reuse `repair_agent.mcp_tools`), apply `extracts`, render `file` and `edits`. No LLM. Emits `tool_call` (tier T1) and `extract` events.
- `t2.py: run(playbook, seed, mcp, provider) -> TierResult`: replay steps (no LLM), then one LLM call with no tools: the collected tool outputs + the playbook's edit template with the variables blanked + the expected output schema (`{"file","edits":[{"find","replace"}],"root_cause","confidence"}`). Provider: Claude CLI (`claude -p --tools ""`, JSON output) by default or Groq. Emits `tool_call`, `llm_call`.
- `t3`: the v3 agent through `repair_agent.runner.investigate(seed, feedback, on_event=...)`.
- `router.py: analyze_hybrid(bundle, feedback) -> Proposal`: signature → playbook → tier choice (feedback present → skip T1 for this attempt), fallback chain, `route` events. `Proposal` gains excluded fields `tier_used: str`, `playbook_id: str | None`, `agent_files`, `agent_evidence` (T1/T2 also return their tool outputs as evidence so the grounded-values guardrail works).
- `REPAIR_ANALYZER_MODE=hybrid` routes `analyzer.analyze` to `router.analyze_hybrid`; it becomes the default in `start_demo.sh` on this branch, with `REPAIR_AGENT_PROVIDER=claude-cli` default.

## 4. Learning hook

In the terminal-phase handler (`verify_handlers.closed_step`) for `analyzer_mode == "hybrid"`: on Healed → if tier_used T3 and merged file equals proposal → `capture` (new playbook or `observed` +1 on existing with consistency update); if T2/T1 → count `t2_ok`/`t1_ok`; then `maybe_promote`. On Failed/Escalated after merge → demote. Emit `learn` events for every decision, including "skipped: reviewer edited the fix".

## 5. Dashboard (trace pop-up and knowledge view)

- Incident card/drawer: a **"Trace"** button opening a full-height modal:
  - header: incident, fault, current phase, **tier badge** (T1 green "deterministic, no LLM", T2 amber "hybrid, 1 LLM call", T3 purple "agent"), tokens, duration;
  - a **flow strip** of stages (Detected → Evidence → Route → Investigate/Replay → Proposal → Guardrails → PR → Review → Sync → Verify → Outcome → Learn), each coloured by status, clickable to scroll;
  - a **timeline**: one row per event, indented by `parent`, icon by kind, title, tier badge, duration; expanding a `tool_call` shows server/tool, **input args** and **output** in two monospace panes with copy buttons; `llm_call` shows prompt and response; `guardrail` rows ✓/✗ with detail; `learn` rows highlighted ("Captured playbook pb-xxxx from this run", "Promoted pb-xxxx T3 → T2");
  - auto-refresh every 2 s while the incident is not terminal.
- A **Knowledge** panel/tab: table of playbooks (signature short, class, state badge, runs, consistency, action sequence chips, last event); click → its history and the steps/extracts/edit template.
- Plain JS + CSS in the existing static app; match the existing style; must be readable on a projector (min 14px font, high contrast).

## 6. Faults

The fault list is extended from SREGym, Cloud-OpsBench and production incident studies (`research/fault-catalog-master.md`), and new faults F17+ are added to `src/repair_faults/catalog.yaml` when they (a) produce a detectable failure in our demo app, (b) need no app-code change, and (c) are fixable or correctly escalatable through git. Each catalog entry gains `evidence_milestones` (2–4 short facts a correct investigation must establish) for later process scoring.

## 7. Testing

Light unit tests with fakes for flow, playbooks (capture, extract rules, promotion/demotion), tier executors (with a fake MCP toolbox), router fallback, dashboard endpoints. Live testing is done by separate test agents (Haiku) with the demo running.

## 8. Implementation notes (engine, issue #25)

Deviations and details of the implemented engine (`playbooks.py`, `crystallize.py`, `tiers/`):

- `Playbook` has an extra `meta` dict (`t1_blocked`, `seen`/`same` for consistency, `streak`, `root_cause`); `runs` also counts `rejected` and `t2_match`. `Extract.from_step` is a 0-based index into `steps`. Commit shas used as tool arguments are bound dynamically (`sha_<n>` extract, `json_path`), because the sha changes per incident. `${` in literal text is escaped as `$${`.
- Tier `run` functions are async (they use the MCP toolbox): `t1.run(playbook, seed, mcp, tracer)`, `t2.run(playbook, seed, mcp, llm, tracer, feedback)`; `mcp` is anything with `async call(name, args) -> (text, is_error)`.
- Hybrid proposals from every tier have `source="agent"`, `tier=3`; the tier is `Proposal.tier_used` (`T1`/`T2`/`T3`) and `playbook_id`. `repair_flow._agent_status` writes `status.agent.tierUsed`, `playbookId`, `rulesMatch`. The learner reads those fields and the saved T3 trace `agent-traces/<incident>-a<attempt>.json`.
- `route` event data: `chosen_tier` (also `tier`), `playbook_id`, `playbook_state`, `reason`, `fallback_from`, `counts`.
- Promotion: T3 -> T2 needs `t3_ok >= REPAIR_PROMOTE_T2_AFTER`, `streak >= that` (clean verified T3 heals since the last state change), `consistency >= 0.9`, `edited == 0`, `rejected == 0`. T2 -> T1 needs `streak >= REPAIR_PROMOTE_T1_AFTER` where only T2 heals whose LLM edit equalled the extract-rule edit count (a non-matching T2 heal resets the streak), and no `t1_blocked`. `t2_ok` counts all verified T2 heals, `t2_match` the matching ones.
- Only clean heals count: merged change equals the proposal and `attempts == 1` with no review feedback; otherwise the learner emits `skipped` (and bumps `rejected` / `edited` on an existing playbook).
- Demotion: failed T1/T2 execution (tool error, extract failure, find anchor not exactly once in the replayed file, invalid LLM output) demotes in the same attempt and falls through to the next tier; the router also demotes when a retry arrives with feedback and the previous attempt ran on T1/T2 (covers guardrail rejections and reviewer change requests); the learner demotes on Failed/Escalated (PR closed or re-run failed) and on a reviewer edit. Each demotion resets `streak`.
