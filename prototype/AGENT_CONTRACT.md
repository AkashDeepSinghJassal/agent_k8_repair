# Prototype v3 contract: Approach C (agent over MCP)

Branch `prototype-v3`. All subagents build against this file. If you must deviate, change this file in the same edit and say so in your report.

## Idea

The operator (unchanged control plane: detect, incident record, guardrails, PR, verify) no longer asks the LLM once with a fixed evidence bundle. In `REPAIR_ANALYZER_MODE=agent` it hands a short **seed** to an **agent**. The agent investigates by calling **read-only tools on MCP servers** and finishes by calling `propose_patch` or `escalate` on the `repair` MCP server. `propose_patch` only records the proposal; the operator then runs the existing guardrails, opens the PR, a human merges, Argo CD syncs, the operator verifies.

Two interchangeable agent hosts (the "LLM provider" switch):

- `groq`: our own loop in Python. MCP client (official `mcp` SDK, **v2.3**, stdio) + Groq chat completions with tool calling.
- `claude-cli`: the local `claude` binary (`claude -p`) is the agent host. It connects to the same MCP servers through `--mcp-config`. Uses the user's Claude subscription (strip `ANTHROPIC_API_KEY` from the child env).

## Packages and files

```
src/repair_mcp/            MCP servers (one module each, runnable: python -m repair_mcp.<name>)
  common.py                allowlists, redaction (reuse repair_operator.evidence.redact), truncation, clients
  k8s_server.py            server name "k8s"
  git_server.py            server name "git"
  argo_server.py           server name "argo"
  repair_server.py         server name "repair" (output channel)
src/repair_agent/
  config.py                provider selection
  prompt.py                system prompt + seed rendering
  servers.py               the 4 server launch specs (shared by both hosts)
  groq_host.py             own loop
  claude_host.py           claude -p host
  runner.py                investigate(...) -> AgentResult, fallback between providers
  __main__.py              CLI: python -m repair_agent investigate --namespace demo --pod <pod> [--provider ...]
src/repair_operator/analyzer/agent.py   operator adapter (bundle -> seed -> Proposal)
```

mcp 2.x note: `FastMCP` is now `from mcp.server.mcpserver import MCPServer`. Check the installed package (`.venv/lib/python3.12/site-packages/mcp`) for the exact API; do not guess v1 APIs.

## MCP tools (all read-only except the repair server's two)

Environment for every server: `REPAIR_KUBE_CONTEXT` (default `kind-repair`), `REPAIR_AGENT_NAMESPACES` (default `demo,demo-b`), `REPAIR_GITHUB_TOKEN`, `REPAIR_DEMO_REPO` (default `AkashDeepSinghJassal/repair-demo-app`), `REPAIR_AGENT_REPOS` (comma list, default = demo repo), `REPAIR_DEMO_BRANCH` (default `main`).

Every tool: returns JSON-serialisable data or text; errors are returned as `{"error": "..."}` (never raise to the client); text is redacted with `repair_operator.evidence.redact`; large outputs truncated (logs: last 200 lines; files: 400 lines) with a note.

### `k8s`
| Tool | Args | Returns |
| --- | --- | --- |
| `k8s_list` | `kind`, `namespace`, `label_selector=""` | names + one-line status. kinds: pods, deployments, replicasets, services, endpoints, configmaps, networkpolicies, events, persistentvolumeclaims |
| `k8s_get` | `kind`, `namespace`, `name` | the object as a dict without `managedFields`. `secrets`: key names only, never values |
| `k8s_describe_pod` | `namespace`, `pod` | phase, conditions, per-container state / lastState / restartCount / image / resources, node, owner |
| `k8s_logs` | `namespace`, `pod`, `container=""`, `previous=False`, `tail=200` | redacted lines |
| `k8s_events` | `namespace`, `name=""` | recent events (type, reason, object, message, count, lastTimestamp) sorted newest first, max 50 |

Namespace must be in the allowlist, else `{"error": "namespace not allowed"}`.

### `git`
| Tool | Args | Returns |
| --- | --- | --- |
| `git_list` | `prefix="deploy/"`, `repo=""` | file paths under prefix |
| `git_read` | `path`, `ref=""`, `repo=""` | file text |
| `git_log` | `path`, `limit=5`, `repo=""` | `[{sha, date, message}]` for commits touching the path |
| `git_diff` | `path`, `sha=""`, `repo=""` | unified diff of that file in commit `sha` (default: last commit touching it) |

`repo=""` means the default repo. Repo must be in `REPAIR_AGENT_REPOS`. Path must match `deploy/**` or `workflows/**` (`.yaml`/`.yml`).

### `argo`
| Tool | Args | Returns |
| --- | --- | --- |
| `argo_app_for_namespace` | `namespace` | `{app, repoURL, path, targetRevision}` from the Argo CD Application whose destination is that namespace (namespace `argocd`). For multi-repo resolution |
| `argo_app_status` | `app` | sync status, revision, health, per-resource health/status message |
| `workflow_status` | `namespace`, `name` | phase, message, nodes `[{name, displayName, phase, message, podName, template}]` |

### `repair` (output channel; the only "write", and it writes only a local JSON file)
| Tool | Args | Effect |
| --- | --- | --- |
| `propose_patch` | `file`, `edits: [{find, replace}]`, `root_cause`, `failure_class`, `confidence` (0..1), `rationale`, `verify=""`, `repo=""` | Validates (non-empty edits, find != replace, path pattern as git). Writes `{"status": "proposed", ...args}` to the path in env `REPAIR_AGENT_OUT`. Returns `"recorded; your investigation is complete, stop now"` |
| `escalate` | `reason`, `root_cause=""`, `failure_class=""` | Writes `{"status": "escalated", ...}` to `REPAIR_AGENT_OUT`. Same return |

`failure_class` vocabulary: OOM_KILLED, IMAGE_PULL, CONFIG_ERROR, CRASH_LOOP, STAGE_FAILURE, UNSCHEDULABLE, NETWORK, OTHER.

## Server launch spec (`repair_agent/servers.py`)

```python
def server_specs(out_path: str, env: dict | None = None) -> dict[str, dict]:
    # {"k8s": {"command": sys.executable, "args": ["-m", "repair_mcp.k8s_server"], "env": {...}}, "git": ..., "argo": ..., "repair": ...}
```
Same dict feeds the Groq host (stdio clients) and the Claude host (written as `{"mcpServers": ...}` to a temp `--mcp-config` file). `env` must include `PATH`, `HOME`, `KUBECONFIG` if set, the variables above, and `REPAIR_AGENT_OUT` (repair server only needs it).

## Agent API (`repair_agent/runner.py`)

```python
@dataclass
class AgentResult:
    status: str                 # "proposed" | "escalated" | "failed"
    proposal: dict | None       # what propose_patch / escalate recorded
    trace: list[dict]           # [{"step", "tool", "args", "result_preview" (<=500 chars), "ms", "error"?}]
    evidence_text: str          # full text of every tool result, concatenated (used by the grounded_values guardrail)
    files_read: list[str]       # git paths returned by git_read
    usage: dict                 # {"provider", "model", "steps", "tool_calls", "input_tokens", "output_tokens", "latency_ms", "fallback_from"?}
    error: str = ""

def investigate(seed: dict, feedback: list[str] | None = None, *, provider: str | None = None,
                max_steps: int = 30, timeout_s: int = 600) -> AgentResult: ...
```

Seed keys: `incident`, `namespace`, `kind` (Pod|Workflow), `name`, `owner`, `container`, `reason`, `classHint`, `message`, `repo`, `app`, `path` (git path of the owner's manifest if known).

Provider selection order: explicit arg > `<REPAIR_STATE_DIR or ~/.repair-loop>/agent-config.json` `{"provider": "groq"|"claude-cli", "model": "..."}` (the dashboard writes it) > env `REPAIR_AGENT_PROVIDER` > `groq` if `GROQ_API_KEY` set else `claude-cli`.
Models: groq `REPAIR_AGENT_MODEL` default `openai/gpt-oss-120b`; claude-cli `REPAIR_AGENT_CLAUDE_MODEL` default `sonnet`.

"Must always work" rules:
- Step budget 30 tool calls (generous; tokens are not a concern in v3).
- A tool error goes back to the model as the tool result; the loop continues.
- If the model stops without calling `propose_patch`/`escalate`, nudge it (up to 2 times) to finish.
- Groq API errors: 3 retries with backoff. Claude CLI: one retry on a non-zero exit.
- If the chosen provider ends `failed` and `REPAIR_AGENT_FALLBACK` is not `0`, run the other provider once; record `usage.fallback_from`.
- Never raise from `investigate`; return `status="failed"` with `error`.

Claude CLI invocation (check `claude --help` for the exact flags of the installed version 2.1.x):
`claude -p <prompt> --mcp-config <tmp.json> --strict-mcp-config --allowedTools mcp__k8s mcp__git mcp__argo mcp__repair --output-format stream-json --verbose --max-turns 40 --model <model>`, with the system instructions appended via `--append-system-prompt` (or in the prompt), cwd a temp dir, env without `ANTHROPIC_API_KEY`. Parse `tool_use` / `tool_result` blocks from the stream into `trace`, usage from the final `result` event.

## Operator integration

- `REPAIR_ANALYZER_MODE=agent` routes `analyzer.analyze` to `analyzer/agent.py: agent_analyze(bundle, feedback)`. Pure Approach C: no recipe tier, no rule tier.
- `Proposal.source` gains `"agent"`. Proposed → tier 3 (edits); escalated or failed → tier 4. New excluded fields on `Proposal`: `agent_meta: dict | None` (usage + trace), `agent_evidence: str = ""`, `agent_files: list[str] = []`.
- `repair_flow.analyze_step`: when `proposal.source == "agent"`, the guardrail evidence text is `evidence_text(bundle) + proposal.agent_evidence`, and allowed files are `allowed_files(bundle)` plus `agent_files` that match the git path pattern. The file the agent patches must be one it read with `git_read` or one in the bundle.
- The trace is saved to `<state_dir>/agent-traces/<incident>-a<attempt>.json`; the incident status gets `agent: {provider, model, steps, tools: [names in order], fallbackFrom?}`.
- Feedback (guardrail rejection reasons, reviewer comments, failed verification) is passed to `investigate` as `feedback`.

## Dashboard

- `GET /api/agent/config` → `{provider, model, available: {groq: bool, "claude-cli": bool}}`; `POST /api/agent/config` `{provider, model?}` writes `agent-config.json`.
- Header control to switch provider.
- Incident detail: "Agent investigation" section listing the trace (step, tool, args, short result), provider/model/steps.

## Demo fault for the agent

Add a fault whose cause is **not** in the operator's fixed evidence bundle (so only an investigating agent can find it), e.g. a Service/port or cross-resource reference broken so that a pod crashes. Keep F1–F9 working.

## Testing

Light. Unit tests with fakes for tool logic and the host loops; no network in default tests. Live checks are marked `live`.
