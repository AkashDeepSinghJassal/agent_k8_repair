# Prototype v3: agent over MCP (Approach C)

Status: built on branch `prototype-v3`, as of 2026-10-27. Single runs only. This doc has no evaluation numbers.

Related: [prototype/AGENT_CONTRACT.md](../prototype/AGENT_CONTRACT.md) (interfaces and as-built notes), [approach-c-and-hybrid.md](approach-c-and-hybrid.md) (the design note this builds on), [prototype/README.md](../prototype/README.md) (setup and run).

## 1. What Approach C is

v2 is Approach A, the one-shot analyzer. The operator collects a fixed evidence bundle (pod status, logs, events, the owner's manifest, related ConfigMaps). The LLM reads the bundle once and returns a template choice or small edits. Seven guardrails check the answer.

v3 keeps the same control plane. Only the analyzer changes. In `REPAIR_ANALYZER_MODE=agent` the operator sends a short seed (namespace, pod, owner, reason, message, the manifest path if known). The agent decides what to read. It calls read-only tools on MCP servers, one step at a time, until it calls `propose_patch` or `escalate`.

| | v2: one-shot analyzer (Approach A) | v3: agent over MCP (Approach C) |
| --- | --- | --- |
| Evidence | Fixed bundle, built by the operator before the model runs | Gathered by the agent with tools, in an order it chooses |
| Model calls per incident | One | One per agent step; the Groq host allows 30 tool calls |
| Output | Template or find/replace edits | Same edits, through `propose_patch`; or `escalate` |
| Causes it can see | Those inside the bundle | Those a tool can reach, including other files and other objects |
| Trace | Prompt and reply saved for replay | Tool trace saved per attempt (`agent-traces/`) |
| Guardrails, PR, human merge, Argo CD sync, verify | Same | Same |

The main change is who chooses the evidence. The model's tool choices are also new data for the research (see R1).

## 2. Architecture

```
  +----------------------------------------------+
  | Operator (Kopf)  = control plane             |
  |  detect, incident record, guardrails,        |
  |  PR, verify. analyzer/agent.py adapter       |
  +-----------+-----------------------+----------+
              | short seed            ^ proposal (JSON file)
              v                       |
  +----------------------------------------------+
  | Agent host  (one of two)                     |
  |  groq:       own Python loop + Groq API      |
  |  claude-cli: `claude -p` (subscription)      |
  +--+--------------+--------------+--------+----+
     | MCP over stdio (same four servers for both hosts)
     v              v              v        v
  +--------+   +--------+   +--------+   +--------+
  |  k8s   |   |  git   |   |  argo  |   | repair |
  | 5 tools|   | 4 tools|   | 3 tools|   | 2 tools|
  | read   |   | read   |   | read   |   | writes |
  +--------+   +--------+   +--------+   | a local|
                                         | JSON   |
                                         | file   |
                                         +--------+
```

The `repair` server is the only write path, and it writes only a local JSON file. `propose_patch` records a proposal. It does not open a PR and it does not touch the cluster or git. After that:

1. The operator reads the proposal. The edit's file must be one the agent read with `git_read`, or one in the bundle.
2. The seven guardrails run, in order. The `grounded_values` check uses the bundle text plus the text of every tool result.
3. If all pass, the operator opens a PR on `repair-demo-app` (`deploy/**` or `workflows/**` only).
4. A human reviews and merges (dashboard **Approve & merge**, or GitHub).
5. Argo CD syncs the merge.
6. The operator checks the verify window. The incident goes to `Healed`, or back to `Analyzing` (a new attempt), or to `Escalated`.

Safety in this path:

- The model cannot write to the cluster or to git.
- The operator's RBAC is read-only.
- Tool output is redacted and size-capped before the model sees it, and it is treated as data.
- Guardrails sit outside the model. A bad proposal is blocked even if the model produces it.

## 3. The 14 tools

All tools are read-only except the two on the `repair` server. Errors come back as `{"error": "..."}` and do not stop the loop. Namespaces must be on the allowlist (`REPAIR_AGENT_NAMESPACES`, default `demo,demo-b`). Git paths must match `deploy/**` or `workflows/**` with a `.yaml` or `.yml` suffix.

| Server | Tool | Args | Returns |
| --- | --- | --- | --- |
| k8s | `k8s_list` | `kind`, `namespace`, `label_selector` | Names and one-line status. Kinds: pods, deployments, replicasets, services, endpoints, configmaps, networkpolicies, events, persistentvolumeclaims |
| k8s | `k8s_get` | `kind`, `namespace`, `name` | The object as a dict, without `managedFields`. Secrets: key names only |
| k8s | `k8s_describe_pod` | `namespace`, `pod` | Phase, conditions, per-container state, last state, restart count, image, resources, node, owner |
| k8s | `k8s_logs` | `namespace`, `pod`, `container`, `previous`, `tail` | Redacted log lines (max 200) |
| k8s | `k8s_events` | `namespace`, `name` | Recent events, newest first (max 50) |
| git | `git_list` | `prefix`, `repo` | Paths under the prefix (default `deploy/`) |
| git | `git_read` | `path`, `ref`, `repo` | File text (first 400 lines, redacted) |
| git | `git_log` | `path`, `limit`, `repo` | Commits that touched the path: sha, date, message |
| git | `git_diff` | `path`, `sha`, `repo` | Unified diff of the file in one commit (default: last commit that touched it) |
| argo | `argo_app_for_namespace` | `namespace` | The Argo CD Application for that namespace: app, repoURL, path, targetRevision |
| argo | `argo_app_status` | `app` | Sync status, revision, health, per-resource health and message |
| argo | `workflow_status` | `namespace`, `name` | Workflow phase, message, nodes (name, phase, message, pod, template) |
| repair | `propose_patch` | `file`, `edits[{find, replace}]`, `root_cause`, `failure_class`, `confidence`, `rationale`, `verify`, `repo` | Writes `{"status": "proposed", ...}` to the file in `REPAIR_AGENT_OUT`. **Write**, local file only. Ends the run |
| repair | `escalate` | `reason`, `root_cause`, `failure_class` | Writes `{"status": "escalated", ...}`. **Write**, local file only. Ends the run |

`failure_class` values: OOM_KILLED, IMAGE_PULL, CONFIG_ERROR, CRASH_LOOP, STAGE_FAILURE, UNSCHEDULABLE, NETWORK, OTHER.

The design note (section 2) also sketched `configmap_diff` and `scone_*` tools. Neither is in the built set. `scone_*` belongs to the enclave step.

## 4. Agent hosts and the provider switch

Two hosts run the same four servers, so the tools are the same for both.

| Host | How it works | Notes |
| --- | --- | --- |
| `groq` | Own loop in Python. The official `mcp` SDK is the client (stdio). Groq chat completions with tool calling | Retries Groq errors 6 times (`API_RETRIES = 6` in `groq_host.py`, each wait follows the server's hint). Shortens old tool results to fit the per-request limit (`REPAIR_AGENT_CTX_TOKENS`, default 5500) |
| `claude-cli` | Runs `claude -p` with `--mcp-config`, `--strict-mcp-config`, `--tools ""`, `--allowedTools mcp__k8s mcp__git mcp__argo mcp__repair`, `--permission-mode dontAsk` | Uses the local Claude login. `ANTHROPIC_API_KEY` is removed from the child env. One retry on a non-zero exit |

**Selection order.** The first valid value wins:

| Order | Source |
| --- | --- |
| 1 | Explicit argument (`--provider`, or the `provider` argument of `investigate`) |
| 2 | `agent-config.json` in the state dir (`REPAIR_STATE_DIR`, default `~/.repair-loop`). The dashboard writes it |
| 3 | `REPAIR_AGENT_PROVIDER` |
| 4 | `groq` if `GROQ_API_KEY` is set, else `claude-cli` |

Models: groq uses `REPAIR_AGENT_MODEL` (default `openai/gpt-oss-120b`). claude-cli uses `REPAIR_AGENT_CLAUDE_MODEL` (default `sonnet`). A model in `agent-config.json` applies only to the provider it was saved for.

**Dashboard switch.** The dashboard header has a provider control. It calls `POST /api/agent/config` with `{provider, model?}`, which writes `agent-config.json`. The switch takes effect on the next agent run, with no restart. `GET /api/agent/config` reports which providers are available.

**Automatic fallback.** If the chosen provider ends `failed` and `REPAIR_AGENT_FALLBACK` is not `0`, the runner runs the other provider once. `usage.fallback_from` records the first provider, and the error text names both. A run that falls back is a different run for the research. Record the provider that actually produced the result.

**Groq free-tier limit (honest note).** The limits below are the ones this account hit, as seen in Groq 429 and 413 errors during testing. They are not official published figures. A 429 reported 199,724 of 200,000 tokens per day used. The per-request limit of about 8k tokens (the 8000 TPM noted in the comment in `groq_host.py`) is also the reason for the context shortening above. The daily limit is not in the code as a number. The code treats a long retry hint (over 90 s) as a daily-quota signal and fails at once, so the runner falls back. A long agent run late in the day can therefore use up the quota and fall back to `claude-cli`. Two results are then not comparable without the provider field.

## 5. Why F10 exists

Fault F10 is the case the fixed bundle cannot solve. Commit `8e10b28` (PR #19) added it.

- The `api` Service in `deploy/demo/api.yaml` is renamed to `api-svc`.
- The frontend nginx config (a ConfigMap, unchanged) still does `proxy_pass http://api:8000/`.
- nginx resolves that name when it starts. The running frontend pod keeps working. A restarted pod exits with "host not found in upstream" and goes into CrashLoopBackOff.
- The failing pod's manifest (`frontend.yaml`) is correct. The cause is in `api.yaml`, a different object in a different file. The fixed bundle covers the failing pod's own manifest and the ConfigMaps it references, not the Service.

So a fixed bundle cannot see the cause. An agent has a path to it: the crash shows in the pod logs, the frontend ConfigMap points at `api`, and a Service list shows no `api`. Whether the agent takes that path is the test for R2 and R1. The fix is `api-svc` back to `api` in `api.yaml`.

End-to-end repair runs are recorded live for F1 to F9. F10 to F16 were injected and their symptoms observed live on the kind cluster when they were added (see `failure-taxonomy.md`). Their end-to-end repair runs are recorded in `prototype-v3-results.md` (being written).

| ID | Title (from the catalog) | File with the cause | Expected class |
| --- | --- | --- | --- |
| F10 | api Service renamed (frontend cannot resolve upstream) | `deploy/demo/api.yaml` | CRASH_LOOP |
| F11 | frontend-nginx ConfigMap key renamed (subPath mount breaks) | `deploy/demo/configmaps.yaml` | CRASH_LOOP |
| F12 | api-b-config ConfigMap renamed in demo-b | `deploy/demo-b/configmaps.yaml` | CONFIG_ERROR |
| F13 | liveness probe on wrong port | `deploy/demo-b/api-b.yaml` | CRASH_LOOP |
| F14 | worker resource request larger than any node | `deploy/demo/worker.yaml` | UNSCHEDULABLE |
| F15 | redis PVC with non-existent StorageClass | `deploy/demo/redis.yaml` | UNSCHEDULABLE |
| F16 | runAsNonRoot on an image that runs as root | `deploy/demo/worker.yaml` | CONFIG_ERROR |

The "file with the cause" column is the file the catalog's `target_file` names. Several of these faults are a second file away from the failing pod, which is the point of F10 to F12.

## 6. Research questions

The thesis asks five research questions (R1 to R5). This table says what v3 records for each. It states no results.

| RQ | Question (generic) | What v3 records |
| --- | --- | --- |
| R1 Evidence | Which evidence does diagnosis need? | Tool names, order, args, and files read, per run (`trace`, `files_read`) |
| R2 Diagnosis | Is the root cause and class right, agent vs one-shot? | Proposal: `root_cause`, `failure_class`, `confidence`, edits. Compared by hand against `correct_fix` in the catalog |
| R3 Latency and cost | How long does a run take, and how many tokens does it use? | `usage`: steps, tool calls, input and output tokens, latency, provider, model, `fallback_from` |
| R4 Repair | Does the fix heal the workload through a reviewed PR? | PR link, phase changes, verify outcome (`Healed`, `Escalated`) |
| R5 Safety | Can injected text or a bad proposal cause harm? | Guardrail results (`rejectedBy`), the tool calls made after a poisoned log line, and the F8 outcome |

R5 is harder with tool choice. A log line can now steer the next tool call, not just the final answer. The guardrails still run on every proposal, but the trace is where injection shows up.

## 7. Deferred

**v4 (next step):**

- **H1, escalate to the agent.** Run the one-shot analyzer first. Hand the incident to the agent only on a guardrail rejection, low confidence, or an unknown class. The default path stays the safe, cheap one. This tests whether the agent rescues cases the one-shot misses, and at what cost.
- **Token minimisation.** Smaller seeds, capped tool results, and fewer steps on the agent path. Measured with R3.
- **H3, crystallising traces.** A verified agent trace becomes a recipe. A repeated fault then runs at tier 0 with no tokens. A failed verify demotes the recipe. The demotion path already exists for recipes.
- **H2, agent as evidence collector** (ablation only). The agent reads and returns evidence, and the one-shot analyzer decides. Useful if injection results look bad in C.

**Later:**

- **SCONE enclave.** Run the operator and the tool servers inside an attested environment. Tool servers become part of the trust question. No standard way to attest an MCP server was found (see the design note, section 5).
- **Semantic verification.** Detect a fix that heals the pod but is wrong. Not built. `propose_patch` has a `verify` field. The operator copies it into the PR body as a note for the reviewer. Nothing checks it against the result.

## 8. Known limits

- Single runs, one model per host. No evaluation numbers yet.
- Agent runs are not deterministic. Each fault needs repeated runs before a claim.
- Outside the enclave. Personal access token, not a GitHub App.
- The Groq free-tier quota can force a fallback mid-experiment (section 4).
- `scripts/start_demo.sh` sets the debounce, PR poll, verify window and cooldown to 5 seconds, for a fast demo. Those timings are not valid for R3 or R4 measurements. Use the operator's defaults or set them explicitly.
