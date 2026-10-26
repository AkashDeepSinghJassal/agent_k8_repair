# MCP for SRE and DevOps, from scratch

Date: 2026-10-26. Branch: `plan-v3`.

This note deliberately ignores our current repair-loop design. It starts from zero: what MCP is, how the industry uses it for operations, which research lines matter for a system that "knows everything, finds what went wrong and learns by itself", and then a set of free, sometimes wild, approaches. Similarities with our thesis problem come only at the very end, briefly.

**Source tags.** **[read]** read directly. **[summary]** from a search summary or abstract, not the full text. **[vendor]** a vendor describing its own product. Check numbers in the full papers before citing them.

---

## Part 1. Foundations: what MCP actually is

### 1.1 The problem it solves

Every agent needs to reach many systems: a cluster, a git host, metrics, logs, tickets, chat. Without a standard, each agent writes its own integration for each system (an N×M problem). MCP makes it N+M: a system is wrapped **once** as an MCP server, and any MCP-capable agent can use it.

### 1.2 The three roles

| Role | What it is | Example |
| --- | --- | --- |
| **Host** | The application the user or the automation runs | Claude Code, an IDE, our own operator |
| **Client** | A connector inside the host, one per server | Built into the host's SDK |
| **Server** | A small program that exposes one system | A Kubernetes server, a Prometheus server, a GitHub server |

The model never talks to the server directly. The host decides which tools the model may see and executes the calls. **That makes the host the natural place for policy.**

### 1.3 What a server offers

| Primitive | Who decides to use it | Meaning | Ops example |
| --- | --- | --- | --- |
| **Tools** | The model | Functions with a JSON Schema input and output | `get_pod_logs(ns, pod)`, `query_prometheus(promql)` |
| **Resources** | The host or user | Read-only data addressed by URI | `runbook://payments/high-latency` |
| **Prompts** | The user | Reusable prompt templates | "Investigate this alert" |

Plus extensions (official ones such as **Tasks**, for long-running work) and **input requests** (a server can say "I need more input" mid-call, and the client retries with the answer; for example, asking a human to approve).

### 1.4 How it travels

JSON-RPC over **stdio** (local process) or **Streamable HTTP** (remote service). Since the **2026-07-28** revision [read: [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)], it is stateless: no handshake, no sessions, every request carries its own version and capabilities. A server can therefore run as an ordinary replicated web service behind a load balancer. `server/discover` lets a client ask any server what it supports. OAuth 2.1-style authorization protects remote servers, and trace context (OpenTelemetry) can flow through every call.

### 1.5 What MCP is not

- Not an agent: it does not plan or decide.
- Not a security boundary on its own: tool output is untrusted text that reaches the model.
- Not agent-to-agent: that is **A2A**, a separate protocol. MCP connects an agent to tools; A2A connects agents to each other. Both now sit under the Linux Foundation's Agentic AI Foundation [summary].
- Not a knowledge store: it gives access to systems, not memory. Memory is something you build, possibly exposed through MCP.

### 1.6 How to learn it hands-on (suggested path)

1. Run an existing server locally (Kubernetes or GitHub) inside an MCP host and watch the tool calls.
2. Write a tiny server with the official Python or TypeScript SDK: two tools and one resource.
3. Make it remote (Streamable HTTP), add authorization, add OpenTelemetry.
4. Put a policy proxy in front of it that allows, denies or asks for approval per tool.
5. Write a host loop yourself (model, tool list, call, observe, repeat) to understand where budgets, policy and memory belong.

---

## Part 2. How the industry uses MCP for SRE and DevOps today

### 2.1 MCP servers for every ops system

Observability and incident tools now ship or have community MCP servers: Grafana (Loki, Prometheus, dashboards), Datadog, Sentry, PagerDuty, Kubernetes, GitHub, Argo CD [summary: directories and vendor docs]. Datadog's Bits AI SRE agent reads Grafana data sources through Grafana's MCP server [vendor]. Grafana's own advice is to **instrument the MCP servers themselves**, because otherwise you cannot tell whether the agent, the server or the backend failed [vendor].

### 2.2 Cloud vendors' SRE agents

| Product | Status | What it does |
| --- | --- | --- |
| Azure SRE Agent | GA 10 March 2026 [summary] | Takes incidents from Azure Monitor, PagerDuty or ServiceNow, investigates, proposes or applies mitigations; changes need human approval [summary]. Reaches AWS through the AWS MCP server [summary]. |
| AWS DevOps Agent | GA 31 March 2026; Azure and on-prem from May 2026 [summary] | Investigates incidents from paging and observability tools; reportedly uses past incidents [summary]. |
| Google Gemini Cloud Assist investigations | Limited public detail [summary] | Root-cause observations and next steps; acts only with explicit authorization. |
| Dynatrace Cloud SRE Agent | [vendor] | Routes a problem to the right cloud vendor's agent and merges findings. |

The shape is converging: **trigger (alert) → investigate across many tools via MCP → propose → human approves → act**. Memory is advertised ("persistent operational knowledge") but I found no public description of how any of them implement it.

### 2.3 Patterns that keep appearing

1. **Read-only by default.** Investigation tools get read-only access; writing is a separate, gated step.
2. **Policy outside the model.** An MCP gateway or firewall applies allow, deny or approve rules (CEL, OPA) to each tool call, because instructions in a prompt can be argued around [vendor, consistent across sources].
3. **Human-on-the-loop.** People set rules and error budgets and intervene by exception, instead of approving every step [summary, CNCF forecast].
4. **Agent-of-agents.** One coordinator routes to specialised agents (per cloud, per system).
5. **Too many tools hurts.** Models pick tools worse when shown more than about 25–50 at once [vendor guide], so large estates need tool search or routing.

---

## Part 3. Research lines for "knows everything, finds the fault, learns"

### 3.1 Knowing the system: graphs constrain the agent

The strongest recent RCA results all **give the agent the service or causal graph and make it reason along it**, instead of letting it wander:

- **GALA+** ([arXiv 2608.08968](https://arxiv.org/pdf/2608.08968)): dependency graph bounds the agent's exploration; reports more than 25 points better top-1 accuracy than the best LLM baseline on two microservice benchmarks [summary].
- **MetaRCA** (FSE 2026, [arXiv 2603.02032](https://arxiv.org/pdf/2603.02032)): a "meta causal graph" built from LLM knowledge, past fault reports and observability; instantiated per incident and pruned with live data; 252 public and 59 production failures [summary].
- **AMER-RCL** (ICSE-SEIP 2026, [arXiv 2601.02732](https://arxiv.org/html/2601.02732v1)): turns each alert into a graph of the request path and **reuses reasoning across alerts** (agentic memory) [summary].
- **TopoEvo** ([arXiv 2605.15611](https://papers.cool/arxiv/2605.15611)): argues topology-blind agents blame the loudest downstream victim instead of the cause [summary].

### 3.2 Learning from experience without retraining

- **Reflexion**: retry one task after reflecting on the failure. No memory across tasks.
- **ExpeL** ([arXiv 2308.10144](https://arxiv.org/html/2308.10144v3)): collect successes and failures, extract general insights, reuse them on new tasks; works with closed models [summary]. Weakness: insights are pasted into every prompt, which scales badly.
- **Experiential Reflective Learning** ([arXiv 2603.24639](https://arxiv.org/pdf/2603.24639)): a pool of reusable heuristics retrieved by relevance; +7.8 points over ReAct on Gaia2, better than ExpeL [summary].
- **Voyager-style skill libraries**: store working procedures as code and reuse them.
- **Skill lifecycle survey** ([arXiv 2607.10113](https://arxiv.org/pdf/2607.10113)): skills need verification, retrieval, pruning of stale entries and provenance with rollback [summary].
- **Progressive Crystallization** ([arXiv 2607.07052](https://arxiv.org/pdf/2607.07052)): agent exploration is turned into hybrid, then deterministic workflows; deterministic share reached about 45% and per-incident cost fell over 70% in one production system [read via fetched summary].

Industry precedent: Microsoft's **RCACopilot** ([paper](https://www.microsoft.com/en-us/research/publication/automatic-root-cause-analysis-via-large-language-models-for-cloud-incidents)) predicts the root-cause category using the top-K similar past incidents as examples; accuracy up to 0.766 on a year of incidents [summary].

### 3.3 Testing fixes before reality: twins and sandboxes

- **NetArena** ([arXiv 2506.03231](https://arxiv.org/pdf/2506.03231)) runs a kind cluster as a faithful simulator; the agent diagnoses injected faults, applies a command, sees the new state [summary].
- **Cloud-OpsBench** ([arXiv 2603.00468](https://arxiv.org/pdf/2603.00468)) builds a deterministic "digital twin" from state snapshots: 452 faults, 40 root-cause types [summary].
- I found **no published system that learns a model of "change → effect" for Kubernetes** and uses it to predict a fix's outcome before applying it.

### 3.4 Generating failures to learn from: agentic chaos

- **ChaosEater** ([arXiv 2511.07865](https://arxiv.org/pdf/2511.07865)): an LLM automates the whole chaos-engineering cycle: hypothesis, experiment, analysis, improvement [summary].
- **ReliabilityBench** ([arXiv 2601.06112](https://web3.arxiv.org/pdf/2601.06112)): agents at 96.9% drop to 88.1% when tools time out, rate-limit or drift; simple ReAct agents held up better than Reflexion [summary].
- Nothing found that closes the loop (an agent breaks things, another learns to fix them, the knowledge accumulates).

### 3.5 Tools that grow themselves

- **AutoMCP** ([arXiv 2507.16044](https://arxiv.org/html/2507.16044v1)): compiles MCP servers from OpenAPI specs; 76.5% of 1,023 sampled tool calls worked out of the box [summary].
- Vendors generate tools on the fly from API docs [vendor]. Runtime tool-writing by the agent itself is not established.

### 3.6 Always-on agents

"Ambient agents" react to an event stream and act without a prompt (Google ADK documents the pattern [vendor docs]). Kubernetes watch streams are the natural source; the hard parts are deduplication, restarts and cost, which no source covers well.

---

## Part 4. Free approaches, including wild ones

Each is a possible thesis or chapter on its own. Ordered roughly from conservative to wild.

### W1. The "ops brain": one living knowledge graph served over MCP

Build a graph of everything: services, deployments, repos, config, owners, SLOs, dependencies, past incidents and their fixes. Feed it continuously from Kubernetes watches, git, Argo CD, CI and tracing. Expose it as **one MCP server** with tools such as `blast_radius(service)`, `what_changed(since)`, `similar_incidents(signature)`, `who_owns(resource)`.

Every investigating agent queries the graph first; every resolved incident writes back an edge ("change X caused failure Y, fixed by Z"). The graph is the memory; agents are disposable.

- Builds on: GALA+, MetaRCA, RCACopilot.
- New: graph-as-a-service shared by all agents, and incidents as first-class graph edges.
- Risk: keeping the graph fresh and correct.

### W2. Self-play chaos school

Two agents in a sandbox cluster. The **breaker** reads the graph and invents plausible faults (wrong config, bad rollout, resource pressure, network policy). The **healer** must diagnose and fix them. A judge checks the fix with real health and functional tests. Every verified (fault, evidence, fix) triple becomes training data and a skill. The breaker is rewarded for faults the healer fails on, so the curriculum gets harder by itself.

- Builds on: ChaosEater, NetArena, skill libraries.
- New: closes the loop between fault generation and repair learning; produces its own labelled dataset.
- Risk: synthetic faults may not resemble real ones; needs a realism check against real incidents.

### W3. Shadow twin: test every fix before reality

On each incident, snapshot the relevant slice of state (manifests, config, versions) into an ephemeral kind cluster, reproduce the failure there, try candidate fixes **in parallel**, and only propose the one that actually heals the twin. Over time, record (state, change, outcome) to learn a predictor of "will this change work?" that ranks candidates before even running them.

- Builds on: NetArena, Cloud-OpsBench snapshots, server-side dry run.
- New: reproduce-then-fix as a gate, and a learned change-effect model. I found nothing published that does the second part.
- Risk: failures that depend on load, data or external services do not reproduce in a twin.

### W4. A council of specialist agents

One agent per domain, each owning its own MCP servers and its own memory: Kubernetes, networking, database, CI/CD, cost, security. A coordinator broadcasts the incident; specialists investigate in parallel and **argue** (debate or vote) about the cause; a judge picks. Agents talk over A2A; each publishes an agent card describing what it knows.

- Builds on: the vendor agent-of-agents pattern, multi-agent debate.
- New: specialists with persistent per-domain memory that improve separately.
- Risk: cost; coordination failures; forged agent identity.

### W5. Self-extending toolbox

When the agent lacks evidence ("I need the connection-pool metrics of service X, and there is no tool"), it **writes a new MCP tool**, from an OpenAPI spec or a small script, runs it in a sandbox, and submits it for review. Approved tools join a catalogue with a tool-search tool on top, so the agent's reach grows with each incident.

- Builds on: AutoMCP, tool-search patterns.
- New: tool creation driven by gaps found during real investigations.
- Risk: generated tools are code with access; they need review, sandboxing and least privilege.

### W6. Postmortem-native learning

The agent drafts the postmortem for every incident. Humans edit it. The **diff between draft and final** is the most valuable training signal there is: it shows exactly where the agent's understanding was wrong. Distill edits into heuristics (ERL style) and retrieve them by similarity for future incidents.

- Builds on: ExpeL, ERL, RCACopilot.
- New: human corrections of narrative as the learning signal, not only fix success.
- Risk: needs real incidents and willing humans; slow feedback.

### W7. Pre-mortem agent: fix it before it breaks

An ambient agent watches every change (PRs, config, image bumps, quota) and, using the graph and past incidents, predicts which change is likely to cause a failure. It comments on the PR before merge ("this lowers the memory limit below the 95th-percentile usage of the last week; two past incidents had this shape").

- Builds on: ambient agents, incident memory.
- New: shifts from repair to prevention; measured by incidents avoided.
- Risk: false alarms erode trust quickly.

### W8. Federated skill sharing between organisations

Each organisation's agent learns skills locally. Skills (not data) are shared: signatures and fix procedures, stripped of secrets, signed, and accepted by others only after verification in their own twin. Every participant gets better from everyone's incidents.

- Builds on: skill provenance and rollback.
- New: a trust model for sharing learned operations knowledge.
- Risk: poisoned skills; leaking structure about private systems.

### Comparing them

| | Novelty | Feasible in a thesis | Needs real incidents | Measurable |
| --- | --- | --- | --- | --- |
| W1 Ops brain | Medium | Yes | Helps, not required | Diagnosis accuracy with vs without graph |
| W2 Chaos school | High | Yes, in kind | No | Repair rate over curriculum rounds |
| W3 Shadow twin | High | Yes for config faults | No | Fix success before vs after the gate |
| W4 Council | Medium | Yes, costly | No | Accuracy vs single agent, cost |
| W5 Self-extending tools | High | Partly | No | Coverage of new fault types |
| W6 Postmortem learning | Medium | Hard alone | Yes | Edit distance over time |
| W7 Pre-mortem | High | Partly | Yes | Incidents prevented, false alarms |
| W8 Federated skills | High | Hard | Yes | Transfer between sites |

My pick, if starting fresh: **W2 + W3 + W1 together**. The breaker generates faults, the healer learns, the twin verifies, and the graph is the shared memory. It needs no real incidents, runs on a laptop with kind, produces its own dataset, and every part is measurable.

---

## Part 5. Risks that apply to all of them

- **Prompt and tool injection.** MCPTox measured 36.5% average attack success over 20 models on 45 real servers [summary]. Logs, events and tool descriptions are all attacker-reachable text.
- **Tool poisoning and supply chain.** Unreviewed servers run with full privileges in some hosts [summary, Cloud Security Alliance note].
- **Wrong lessons.** A fix that "looks healthy" but is wrong becomes a learned skill. Verification quality caps learning quality.
- **Cost.** Multi-step agents make many model calls; councils multiply that.
- **Autonomy creep.** The industry is moving toward auto-remediation; every approach above should keep writing behind a policy gate.

---

## Part 6. Possible links back to our problem (for later)

Only noting them; not evaluating yet.

- W1's graph is a structured version of "evidence" that any repair loop needs.
- W2 produces labelled faults, which is exactly what a fault dataset needs, and could include confidential-computing faults.
- W3's twin is a stronger form of verification than a health window.
- W8's trust question (who may share a learned fix, with what proof) resembles attestation.

---

## Sources

Read directly: [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog).

Research (search summaries): [GALA+](https://arxiv.org/pdf/2608.08968), [MetaRCA](https://arxiv.org/pdf/2603.02032), [AMER-RCL](https://arxiv.org/html/2601.02732v1), [TopoEvo](https://papers.cool/arxiv/2605.15611), [ExpeL](https://arxiv.org/html/2308.10144v3), [Experiential Reflective Learning](https://arxiv.org/pdf/2603.24639), [Dynamic Agent Skills survey](https://arxiv.org/pdf/2607.10113), [Agent memory survey](https://arxiv.org/pdf/2602.06052), [Progressive Crystallization](https://arxiv.org/pdf/2607.07052), [RCACopilot](https://www.microsoft.com/en-us/research/publication/automatic-root-cause-analysis-via-large-language-models-for-cloud-incidents), [NetArena](https://arxiv.org/pdf/2506.03231), [Cloud-OpsBench](https://arxiv.org/pdf/2603.00468), [ChaosEater](https://arxiv.org/pdf/2511.07865), [ReliabilityBench](https://web3.arxiv.org/pdf/2601.06112), [AutoMCP](https://arxiv.org/html/2507.16044v1), [CSA note on MCP tool poisoning](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/), [MCP vs A2A](https://philippdubach.com/posts/mcp-vs-a2a-in-2026-how-the-ai-protocol-war-ends/), [CNCF 2026 forecast](https://www.cncf.io/blog/2026/01/23/the-autonomous-enterprise-and-the-four-pillars-of-platform-control-2026-forecast/).

Industry and vendor: [Datadog: Grafana MCP](https://docs.datadoghq.com/integrations/grafana-mcp/), [Grafana: monitoring MCP servers](https://grafana.com/blog/ai-observability-MCP-servers), [Azure SRE Agent with AWS DevOps Agent](https://techcommunity.microsoft.com/blog/appsonazureblog/announcing-aws-with-azure-sre-agent-cross-cloud-investigation-using-the-brand-ne/4507413), [AI SRE agents compared](https://infrazen.io/blog/ai-sre-agents-compared-2026), [Google ADK ambient agents](https://adk.dev/runtime/ambient-agents/), [OpenAPI to MCP guide](https://truto.one/blog/openapi-to-mcp-how-mcp-servers-auto-generate-tools-from-api-docs/), [Preloop control plane](https://preloop.ai/resources/ai-agent-control-plane-2026).
