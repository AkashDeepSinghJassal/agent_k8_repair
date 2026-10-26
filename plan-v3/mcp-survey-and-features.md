# Plan v3: MCP and agentic systems, an exploratory survey

Date: 2026-10-26. Branch: `plan-v3`. Status: research notes. Some items are deliberately off-topic; section 5 lists them so we can decide later which fit.

Goal in the user's words: understand how the industry does this today, how MCP helps build agentic systems, and how we could build an agentic operator that talks to all the repositories and services, understands the whole system, repairs failures, and builds knowledge so it improves over time.

Read with [approach-c-and-hybrid.md](approach-c-and-hybrid.md), which covers Approach C and the operator/agent hybrid in detail.

**Source quality.** I read the MCP changelog in full. Everything else comes from search summaries, vendor blogs or abstracts that I did not open in full. Each claim below carries a confidence tag: **[read]** read directly, **[summary]** from a search summary, **[vendor]** a vendor describing its own product.

## 1. What MCP is, and what changed in 2026

MCP is an open protocol that lets a model-based client call external **tools**, read **resources** and use **prompt templates** from servers through one JSON-RPC interface. It moves "how does the agent reach system X" out of the agent and into a reusable server.

The current revision is **2026-07-28** [read: [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)]. The main changes:

| Change | In plain words | Why it might matter to us |
| --- | --- | --- |
| Stateless protocol: no sessions, no `initialize` handshake; every request carries its version and capabilities | Servers become easy to scale and to put behind gateways | An in-cluster MCP server can be a normal Deployment with replicas |
| `server/discover` | A server announces its versions, capabilities and identity | A cheap way for the operator to learn what each repository or service server offers |
| Multi Round-Trip Requests (MRTR): a server returns `input_required`; the client retries with the answers | Replaces server-initiated requests such as elicitation | A tool can ask for human input mid-call, which is a natural fit for our approval step |
| Tasks moved to an official extension (`io.modelcontextprotocol/tasks`), polled with `tasks/get` | Long-running work as a handle you poll | Verification windows and pipeline re-runs are long-running |
| `tools/list` and `resources/list` carry `ttlMs` and `cacheScope`; deterministic tool order | Clients can cache tool lists and keep prompt caches warm | Fewer tokens and calls per incident |
| OpenTelemetry trace context in `_meta` (`traceparent`, `tracestate`) | One trace across agent and tool servers | Direct support for per-stage latency (R3) |
| Authorization tightened: `iss` check, credentials bound to the issuing server, Client ID Metadata Documents preferred over Dynamic Client Registration | Fewer token-confusion attacks | Matters once the operator talks to several servers with different identities |
| Roots, Sampling and Logging deprecated | The server no longer borrows the client's model; the client calls the model provider directly | Matches our design, where the operator owns the LLM call |
| Extensions framework with a feature lifecycle and a 12-month deprecation window | A stable way to add optional features | Our own tools could ship as an extension |

**What MCP does not do.** It does not decide what the agent should do, does not make tool output trustworthy, and does not give any guarantee about the server's code. Those are our job (guardrails, policy, attestation).

## 2. How the industry builds agentic systems around MCP

**Layers that have emerged** (the first two [read] or [summary]; the rest mostly [vendor]):

1. **Tools (MCP).** Agent to systems. Kubernetes, Argo CD, git, observability and ticketing servers exist as open-source projects.
2. **Skills.** Anthropic released Agent Skills as an open standard in December 2025 [summary: several secondary sources]: a folder with a `SKILL.md` (name, description, instructions) plus scripts and references, loaded only when a task matches ("progressive disclosure"). One source frames skills as the judgment layer above tools: when to call what, in what order, how to read the result.
3. **Agent to agent (A2A).** MCP connects an agent downward to tools; A2A connects agents sideways. Both now sit under the Linux Foundation's Agentic AI Foundation [summary]. A2A agents publish an "Agent Card" describing their abilities; sources flag forged cards as a risk.
4. **Gateways and policy.** An "MCP gateway" or "firewall" sits between agent and tools and applies allow, deny or require-approval rules written in policy languages such as CEL or OPA [vendor]. The consistent point: **policy must be enforced outside the model**, because a prompt can be argued around.
5. **Autonomy ladders.** Industry discussion has moved from human-in-the-loop to human-on-the-loop, where people set rules and error budgets and intervene by exception [summary, CNCF forecast]. Autonomy levels are better treated as enforceable limits than as a maturity score [vendor].

**Kubernetes-specific agents.** HolmesGPT, kagent and K8sGPT (investigation, MCP toolsets); Kubernaut (operator plus agent that picks workflows from a catalog, runs them and checks the effect); a read-only Kubernetes MCP server deployed by GitOps (sovereign-selfheal demo). Details and links are in the Approach C note.

**Benchmarks for agentic diagnosis** [summary]:
- Cloud-OpsBench ([arXiv 2603.00468](https://arxiv.org/pdf/2603.00468)): 452 fault cases over 40 root-cause types on a deterministic digital twin of a Kubernetes environment.
- SREGym ([arXiv 2605.07161](https://arxiv.org/pdf/2605.07161)): a live benchmark for SRE agents. Cited via another paper; I did not open it.
- A multi-dataset benchmark for microservice failure diagnosis ([arXiv 2606.29193](https://arxiv.org/pdf/2606.29193)).
- IBM ITBench, reported at 13.8% of 42 real SRE tasks resolved [vendor blog, secondary]. Treat as a rough signal only.

Takeaway: published agentic work is strong on **diagnosis**, weak on **safe, reviewed repair through git**, and I found none that combines this with confidential computing. That gap is where our thesis sits.

## 3. A self-learning, self-healing agentic operator: design sketch

### 3.1 Talking to every repository and service

The operator should not hard-code each system. Put each behind a small MCP server and let the agent discover them.

```
                        ┌──────────────────────────────────────┐
 failure signal ───────▶│  Repair operator (state, safety)     │
                        │  incident record · phases · policy   │
                        └───────┬───────────────────┬──────────┘
                                │ agent loop        │ writes only via
                                ▼                   ▼
        read-only MCP servers                 propose_patch → guardrails
   ┌────────┬────────┬────────┬────────┐             → PR → human → Argo CD
   │ k8s    │ Argo CD│ git /  │ CI /   │
   │ events │ apps   │ code   │ Argo   │
   │ logs   │ health │ search │ Workfl.│
   └────────┴────────┴────────┴────────┘
        ▲ per-repo servers, found via server/discover
```

- **Which repo for which workload.** The Argo CD Application already records `repoURL` and `path` for each workload, so the operator can resolve "this failing pod belongs to repo X, path Y" without configuration. This is the planned fix for the multi-repo gap in the deck.
- **Cross-repo understanding.** Code search across repositories exists as an MCP server (Sourcegraph's, enterprise plans [vendor]; community wrappers are narrower). For our scale a lighter option is enough: an index built from manifests (which service calls which, which ConfigMap feeds which Deployment).

### 3.2 Knowledge that grows: three kinds of memory

The agent-memory surveys [summary: [arXiv 2602.06052](https://arxiv.org/pdf/2602.06052), [arXiv 2607.10113](https://arxiv.org/pdf/2607.10113)] distinguish what to remember. Mapped onto the prototype:

| Memory | Holds | In our prototype today | What would be added |
| --- | --- | --- | --- |
| Episodic | What happened in each incident | `RepairIncident` records, evidence, replay files | A searchable store of past incidents with their agent traces |
| Semantic | Facts about the system | Nothing explicit | A service and dependency graph built from manifests and Argo CD apps |
| Procedural | How to fix a class of failure | Recipe store (candidate, verified, promoted) | Skills: versioned procedures with tests, provenance and rollback |

The skills survey frames the lifecycle as evidence collection, candidate verification, retrieval, pruning of stale entries, and provenance with rollback [summary]. Our recipe store already has the first three in simple form and demotion on failure.

### 3.3 The learning loop

Progressive Crystallization ([arXiv 2607.07052](https://arxiv.org/pdf/2607.07052)) [read, via a fetched summary] is the closest recipe: novel failures go to a free-roaming agent (about 10k–50k tokens); a verified trace is captured as a template; stable steps become rules; a circuit breaker demotes a playbook that regresses. In its production case the deterministic share reached about 45% and per-incident agent cost fell over 70% over eight months. One organisation, no controlled comparison.

For us: the **human's edits and rejections on pull requests** are an extra learning signal the paper does not use. A rejected or edited fix could demote a recipe or be stored as a counter-example. This is also the thesis's "improves with human input" point.

## 4. Where the prototype already stands against this

| Industry element | Prototype status |
| --- | --- |
| Read-only evidence tools | Fixed evidence bundle, not agent-selected tools |
| Policy outside the model | Seven guardrails and human approval: built |
| Write only through reviewed git | Built |
| Learning from verified fixes, with demotion | Built (recipe store); promotion demoed at a lowered threshold |
| Agent loop with tool use | Not built |
| Multi-repo | Not built (one repo, two namespaces) |
| Dependency graph / semantic memory | Not built |
| Per-stage tracing | Custom event log; no OpenTelemetry |
| Semantic verification | Health-only; roadmap item |

## 5. Candidate features to evaluate later

Some are off-topic on purpose. "Fit" is my first guess, to be argued with, not a decision.

| # | Feature | Source | What it would give | Fit | Effort |
| --- | --- | --- | --- | --- | --- |
| 1 | Agent loop over read-only MCP tools | Approach C | Evidence on demand | High | Medium |
| 2 | Escalate to the agent only when one-shot fails | Hybrid H1 | Cost control, safe default | High | Low |
| 3 | Crystallize agent traces into recipes | Crystallization paper | Cost falls as it learns | High | Medium |
| 4 | OpenTelemetry trace context across stages | MCP 2026-07-28 | Real latency data for R3 | High | Low |
| 5 | Approval step as an MCP input request (MRTR) | MCP 2026-07-28 | Human in the loop through the same protocol | Medium | Medium |
| 6 | Tasks extension for verification and re-runs | MCP 2026-07-28 | Standard long-running handles | Medium | Low–Medium |
| 7 | `server/discover` plus Argo-CD-derived repo mapping | MCP + Argo CD | Multi-repo without configuration | High | Medium |
| 8 | Dependency graph as semantic memory | Memory surveys | Better diagnosis of cross-service faults | Medium | Medium |
| 9 | Skills (`SKILL.md`) as the recipe format | Agent Skills | Portable, inspectable procedures | Medium | Low |
| 10 | Policy gateway between agent and tools | MCP gateway pattern | Policy outside the model for tool calls | High if #1 is built | Medium |
| 11 | Counter-examples from rejected or edited PRs | Our idea | Learning from human input | High | Low–Medium |
| 12 | Benchmark on Cloud-OpsBench or SREGym | Benchmarks | Comparison with others' numbers | Medium | High (their setups differ from ours) |
| 13 | Confidential MCP gateway in a TEE (cMCP) | cMCP | Tool-call policy inside the enclave | Depends on enclave work | High |
| 14 | A2A with separate specialist agents | A2A | Per-system expert agents | Low for now | High |
| 15 | Fine-tuned small model on outcomes | Approach E | Cheaper, in-enclave model | Stretch | High |

## 6. Risks to keep in view

- **Tool and prompt injection.** MCPTox measured 36.5% average attack success over 20 models on 45 real servers [summary]. Our F8 fault already shows the log-injection case; an agent whose tool choice depends on log text raises the stakes.
- **Tool poisoning and supply chain.** Instructions hidden in tool metadata; some IDEs auto-run project-defined servers with full privileges [summary, Cloud Security Alliance note]. Pin and review tool definitions; run only our own servers.
- **Forged agent identity (A2A cards)** if we ever add agent-to-agent calls [summary].
- **Autonomy creep.** The industry trend is toward auto-remediation; our guardrails and human approval are the thesis's counter-position. Any move toward auto-merge should be earned per failure class with evidence.
- **Learning from wrong fixes.** The prototype's verification is health-only, so a wrong-but-harmless fix can count as healed and get promoted. Semantic verification should come before any real learning claims.
- **Cost and latency.** An agent loop makes many model calls; inside an enclave each call costs more.

## 7. Suggested order, if we pursue this

1. **H1 with a small agent** (features 1, 2, 4): one-shot first, agent on rejection, trace everything with OpenTelemetry. Compare A, agent and H1 on F1–F9 plus faults the fixed bundle cannot see.
2. **Learning** (3, 11): capture agent traces as candidate recipes; use PR rejections and edits as signals.
3. **Multi-repo** (7, 10): resolve repo from Argo CD Applications; a policy gate between agent and tools.
4. **Optional later**: dependency graph (8), skills format (9), MRTR approval (5), benchmark runs (12).

## 8. Questions for the supervisors

1. Is the agent loop (Approach C) in scope as an ablation, or the main contribution?
2. For "talks to all repositories": is the target several GitOps repositories like ours, or a full source-code understanding across a company's repositories? The second is much larger.
3. Is a published benchmark comparison (Cloud-OpsBench, SREGym) expected, or are our own SCONE-specific faults the evaluation?
4. Should human edits to pull requests count as a learning signal in the thesis?

## Sources

Read directly: [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog).
Search summaries (not opened in full): [MCP 2026-07-28 release post](https://blog.modelcontextprotocol.io/posts/2026-07-28/), [MCP vs A2A overview](https://philippdubach.com/posts/mcp-vs-a2a-in-2026-how-the-ai-protocol-war-ends/), [Agent memory survey 2602.06052](https://arxiv.org/pdf/2602.06052), [Dynamic agent skills survey 2607.10113](https://arxiv.org/pdf/2607.10113), [Cloud-OpsBench 2603.00468](https://arxiv.org/pdf/2603.00468), [OpenDerisk 2510.13561](https://arxiv.org/html/2510.13561v2), [Sourcegraph MCP](https://sourcegraph.com/mcp), [Agent Skills overview](https://atlan.com/know/ai-agent/ai-agent-skills/what-are-agent-skills/), [CSA note on MCP tool poisoning](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/), [CNCF 2026 forecast](https://www.cncf.io/blog/2026/01/23/the-autonomous-enterprise-and-the-four-pillars-of-platform-control-2026-forecast/).
Vendor material (claims about their own products): [Preloop](https://preloop.ai/resources/ai-agent-control-plane-2026), [Gravitee](https://gravitee.io/corpus/gen-582/intelligent-agent/agent-autonomy-levels-and-human-in-the-loop-control-models.html).
Earlier note, with the Crystallization, Kubernaut and cMCP details: [approach-c-and-hybrid.md](approach-c-and-hybrid.md).
