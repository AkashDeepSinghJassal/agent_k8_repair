# Autonomous Incident Resolution at Hyperscale: paper overview and relevance to our thesis

**Paper:** Arun Malik, *Autonomous Incident Resolution at Hyperscale: An Agentic AI Architecture for Network Operations*, arXiv [2606.09122](https://arxiv.org/pdf/2606.09122) v1, 8 June 2026, cs.SE. Microsoft Azure Networking. 7 pages, nine sections, one table, six figures.

**Relation to the Crystallization paper.** Same author, same production system. This paper describes the **architecture**: agents, tools, safety, autonomy levels. The later [Progressive Crystallization paper](progressive-crystallization-overview.md) (arXiv 2607.07052) describes how that system makes repeated work cheaper. They share the same headline results: over 90% autonomous resolution for common categories, hours to minutes, under 5% false-positive remediation.

**Our thesis:** Kubernetes-Native AI Repair Loop for Confidential GitOps Pipelines Using SCONE. v2 (one-shot analyzer, branch `prototype`) and v3 (MCP agent, branch `prototype-v3`) repair failures through reviewed pull requests that Argo CD applies.

**How this overview is built.** I read the full PDF. Statements about the paper come from its text. The figures are charts whose underlying numbers are not given in the text, so I quote only numbers stated in words. "Relevance" paragraphs are my analysis for our project.

---

## 1. The paper in one paragraph

At hyperscale, incident volume, speed and complexity outgrow human on-call teams. The paper describes a production architecture for network operations with four layers: orchestration, knowledge, safety, infrastructure. Four specialised agents (Intake, Planning, Execution, Verification) share the work. Operational capabilities are packaged as typed, permissioned, sandboxed **skills**, inspired by MCP. Human expertise is encoded into verified **playbooks**. A layered **safety framework** covers least privilege, blast radius, reversibility and progressive trust. A five-level **autonomy ladder** promotes or demotes the agents per incident category on measured performance. The reported results are qualitative: over 90% autonomous resolution for well-understood failures, resolution time down two orders of magnitude, false-positive remediation under 5%, and no customer-visible impact.

## 2. Why it matters for us, in one paragraph

It is an industry blueprint for the same problem at a much larger scale, and it lines up with our design point by point:
- **Agents and phases:** its four agents map onto our operator's phases.
- **Skills:** its skills are what our MCP servers are.
- **Safety:** its safety principles match our read-only tools, guardrails and GitOps writes.
- **Autonomy:** its autonomy ladder matches the per-class levels in our blueprint.

It is most useful in three ways:
1. As a **checklist of safety and operability features** we lack: blast-radius estimation, automatic rollback of a fix that made things worse, a global circuit breaker, concurrency limits, multi-model consensus.
2. For the **framing**. Two separate things get promoted on evidence: *authority* (this paper) and *execution type* (the Crystallization paper).
3. As a contrast for our thesis. It acts directly on devices; we act only through git, which gives reversibility and an audit trail by construction. It has no confidential-computing angle at all.

Its evaluation is thin, so it is a design reference, not a source of comparable numbers.

---

## 3. Section by section

### Abstract and I. Introduction

**What it says.**
- **A. Challenge:** millions of devices across hundreds of data centres produce a continuous stream of incidents: hardware failures, software bugs, configuration drift, capacity exhaustion, cascades. Human on-call does not scale:
  - **volume** grows with infrastructure;
  - **velocity**: cascades take seconds while human cycles take minutes to hours;
  - **complexity**: dependencies across layers;
  - **knowledge distribution**: expertise sits with a few senior engineers.
- **B. Five generations of tooling:**
  1. manual;
  2. scripted;
  3. rule-based;
  4. AI-assisted (humans decide);
  5. autonomous (agents perceive, reason, plan, act, verify).

  The paper targets the fifth, but explicitly as *progressive* autonomy with safety invariants at every level, not a jump to full autonomy.
- **C. Five contributions:**
  - an end-to-end multi-agent architecture;
  - skills-based tools inspired by MCP;
  - structured knowledge encoding into verified playbooks;
  - a progressive-autonomy framework with automatic promotion and demotion;
  - production lessons.

**Relevance to us.**
- **Where our prototypes sit on the five generations:**
  - v2's rule table is generation 3;
  - v2's one-shot LLM with human review is generation 4 ("humans remain in the loop for decision-making");
  - v3's agent that investigates and proposes is a step toward 5, but with a human approving every write.

  This is a clean way to position the thesis in the introduction.
- **"Configuration drift" is one of their incident sources.** In GitOps, drift between git and the cluster is handled by Argo CD self-heal. Our faults are the opposite case: git itself is wrong, so Argo CD faithfully applies the mistake.

### II. Background and Related Work

**What it says.**
- **AIOps:** anomaly detection, root-cause analysis and alert correlation give decision support but leave execution to humans.
- **Self-healing systems:** they close the loop, but mostly at application level (restart, scale), and usually in a binary mode (fully manual or fully automatic) with no graduated trust.
- **Multi-agent systems:** LLM tool-use (ReAct, Toolformer) makes them practical.
- **Positioning:** the paper combines all of these with skills via MCP-like protocols, for the full lifecycle from detection to verified resolution, with bounded authority at every stage.

**Relevance to us.**
- **The criticism of self-healing applies to Kubernetes itself.** Restarts and rescheduling are application-level healing; they cannot fix a wrong manifest. This is the same argument as our deck slide "Why an LLM when Kubernetes self-heals".
- **"Binary mode, no graduated trust"** is the gap our autonomy ladder (L0 observe to L4) addresses. This paper is a citable industry example of the graduated alternative.

### III. Architecture

#### A. System overview (Fig. 1)

**What it says.** Four functional layers:
- **orchestration:** agent coordination;
- **knowledge:** operational data and the skill registry;
- **safety:** guardrails and constraints;
- **infrastructure:** device and telemetry access.

**Relevance to us.** Our components map onto the same layers:

| Their layer | Ours |
| --- | --- |
| Orchestration | Kopf operator, `RepairIncident` phases |
| Knowledge | recipe store (`knowledge.py`), fault catalog, evidence bundles, agent traces |
| Safety | seven guardrails, policy rules, allowlists in the MCP servers, human PR review |
| Infrastructure | Kubernetes API, GitHub, Argo CD, Argo Workflows (reached through the MCP servers in v3) |

#### B. Agent decomposition

**What it says.** Four agent roles:
1. **Intake:** classify, prioritise, enrich with topology and history, decide whether the incident is within autonomous scope.
2. **Planning:** most likely root cause from symptoms and history; select strategies from the knowledge base; order steps; produce a structured plan with explicit success criteria and abort conditions.
3. **Execution:** acquire device locks and tokens; collect pre-action state; apply steps one by one, checking intermediate results; handle partial failures; record everything for audit and rollback.
4. **Verification:** health checks specific to the incident type; compare against the plan's expected outcome; watch for regression during a bake-in period; trigger rollback on failure; record resolution evidence.

**Relevance to us.**

| Their agent | Our component | Difference |
| --- | --- | --- |
| Intake | detectors + evidence collector (`detectors.py`, `evidence.py`), fingerprint dedupe, platform-namespace filter | We do not yet decide "in scope or not" before analysis. UNSCHEDULABLE is escalated by the rule table, but other out-of-scope cases reach the LLM |
| Planning | v2 analyzer / v3 agent | Our proposal has a `verify` hint, but no explicit success criteria or abort conditions |
| Execution | `git_writer.py` (branch + PR); Argo CD applies after merge | We never touch the cluster: execution is "change git". No device locks needed: git and the PR are the lock |
| Verification | `verify_handlers.py`: Argo CD sync, health window | Ours is health-only. On failure we go back to analysis, but we do **not** roll back the merged change |

- **The verification gap is the most important finding here.** If a merged fix makes things worse, our loop re-analyses and may propose another fix, but it never reverts the bad merge. With GitOps a revert is cheap: a revert PR on the merge commit, which Argo CD then syncs. Adding "open a revert PR when verification fails" would give us the paper's automatic rollback with no new infrastructure.
- **Plan-defined success criteria** would strengthen our verification. The proposal could state what "fixed" means, for example "api pod Ready and no `DB_URL not set` in logs for 2 minutes", and the verifier could check that instead of generic health. This links to our semantic-verification roadmap item and to the Cloud-OpsBench evidence-graph idea.
- **Earlier decision record.** In our plan-v2 comparison we decided not to call our phase handlers "agents", because calling them multi-agent would overstate the design. This paper uses "agent" for the same kind of roles. For the thesis, our wording stays more precise: deterministic phase handlers with one LLM step (v2), or one investigating agent (v3).

#### C. Coordination protocol

**What it says.** Messages between agents have ordered delivery and at-least-once semantics with acknowledgement. An agent that does not answer in time is escalated to humans. Each agent's reasoning state is checkpointed at decision points so it can recover after transient failures.

**Relevance to us.** Kubernetes gives us most of this for free:
- **checkpointing:** the `RepairIncident` custom resource is the durable state, and each phase is a checkpoint;
- **at-least-once:** Kopf retries handlers until they succeed or are dropped;
- **timeout escalation:** the sync timeout, the attempt limit and escalation to `Escalated` cover it.

The bugs we fixed during the v2 demo were exactly coordination problems:
- stale poll handlers retrying thousands of times after an operator restart;
- a new failure swallowed by an old Healed incident with the same name;
- two operators racing on one cluster.

The paper's list is a useful checklist for the robustness section of the thesis.

#### D. Structured knowledge encoding

**What it says.** Tribal knowledge is turned into machine-executable playbooks in five steps:
1. **observation** of humans resolving incidents (commands, decisions, verifications);
2. **extraction** of patterns across similar incidents;
3. **formalisation** into playbooks with preconditions, steps, decision points and verification criteria;
4. **verification** against historical data;
5. **refinement** from agent outcomes and human feedback.

The playbooks are the main knowledge for the planning and execution agents.

**Relevance to us.**
- This is the **human-sourced** version of the Crystallization paper's **agent-sourced** extraction. The thesis can combine both:
  - recipes learned from verified agent runs (Crystallization);
  - corrections from human review: a reviewer's edit to a PR is an "observation" of the human fix (this paper).
- The **"refinement from human feedback"** step matches our existing rule that a heal where the reviewer edited the proposal gets no promotion credit. It also goes further: the edited fix itself could become the recipe.
- **Playbook structure** (preconditions, steps, decision points, verification criteria) is a good schema for our v4 recipes. Today a recipe stores a template name and parameters only.

#### E. Skills-based tool architecture

**What it says.** Agent reasoning is separated from tool execution. Each capability is a discrete, independently versioned **skill**, "inspired by" MCP and plugin runtimes.
- **Composition:** each skill declares an interface contract (typed inputs, outputs and errors), a capability description used for tool selection, its permission requirements, and whether it is idempotent.
- **Registry and discovery:** a skill registry lets the planner find skills by semantic match with the incident, so new skills can be added without changing agent logic.
- **Sandboxed execution:**
  - compute, memory and time budgets;
  - network access only to granted endpoints;
  - outputs validated against the schema;
  - an immutable audit log of inputs, outputs and side effects.

**Relevance to us: this describes our v3 MCP servers, and shows what they still lack.**

| Skill property | Our MCP tools today | Gap |
| --- | --- | --- |
| Typed interface contract | JSON Schema inputs from the MCP SDK; errors returned as `{"error": ...}` | No declared output schema |
| Capability declaration | Tool descriptions (docstrings) the LLM reads | Fine for 14 tools |
| Permission requirements | Namespace, repo and path allowlists in code; read-only service account | Not declared per tool; not checked by a separate layer |
| Idempotency | All read tools are idempotent; `propose_patch` overwrites one file | Fine |
| Registry and discovery | MCP `tools/list` and, in the 2026-07-28 spec, `server/discover` | No semantic selection; not needed at 14 tools, needed if tools grow past roughly 25–50 |
| Resource budgets | Output truncation; agent-level step budget and timeout | No per-call timeout or memory limit |
| Network isolation | None beyond read-only credentials | Servers run as local processes |
| Output validation | None | Could validate before results reach the model |
| Audit log | Agent trace file per incident attempt | Not immutable; no per-call record of side effects |

The paper calls this **governance**: each skill's blast radius and permission scope can be controlled independently. For our confidential setting, governance per tool matters even more. A tool that returns logs or config can leak secrets, which is why our tools redact and never return Secret values.

### IV. Safety Framework

#### A. Design principles

**What it says.** Four principles:
1. least privilege;
2. blast-radius containment: no single action affects more than a bounded number of devices or services;
3. reversibility: every action reversible, with automated rollback;
4. progressive trust.

**Relevance to us.**

| Principle | Ours | Strength |
| --- | --- | --- |
| Least privilege | Agent: read-only tools, view role. Operator: create branch and PR only. Merge: a human | Strong. One weakness: the demo uses the user's own `gh` token, not a narrow GitHub App or fine-grained token |
| Blast radius | Size cap (changed lines), one file, path allowlist, policy rules | Measured in lines, not in affected workloads |
| Reversibility | Every change is a git commit, reversible with `git revert`; Argo CD applies the revert | Reversible by construction, but no *automatic* rollback yet |
| Progressive trust | Per-class autonomy ladder in the blueprint; recipe promotion | Ladder designed, not implemented beyond recipes |

**GitOps gives us reversibility and audit without the snapshot machinery the paper needs for device configuration.** This is a strong argument for the thesis design.

#### B. Layered authorization

**What it says.** Defence in depth through several layers:
- a unique identity per agent instance;
- action classification (read-only, low-risk change, high-risk change, destructive);
- scope restriction (only devices in the agent's assigned scope);
- rate limiting;
- limits on concurrent operations.

**Relevance to us.**
- **Action classification** fits our design directly:
  - read-only: the MCP tools;
  - low-risk change: a PR touching resource limits or an image tag;
  - high-risk change: a PR touching a Service or a trust-relevant config such as a CAS session or MRENCLAVE value;
  - destructive: deleting resources, which we never do.

  Our blueprint already says trust-changing classes should need two approvals.
- **Scope restriction:** we have it (`REPAIR_AGENT_NAMESPACES`, repo allowlist).
- **Rate limiting:** a per-fingerprint cooldown and an attempt limit.
- **Concurrency limits:** we have none. Nothing stops many open repair PRs at once. "At most N open repair PRs per repo or namespace" is a cheap guard.
- **Identity:** all incidents use one token. A GitHub App with per-repo installation tokens would give per-repo identity (on the roadmap).

#### C. Blast-radius calculation

**What it says.** Before any action:
- **topology analysis:** which services and customers would be affected if it fails;
- **redundancy validation:** is there enough spare capacity to lose the target device temporarily;
- **concurrent impact assessment:** could other operations in flight make it worse.

Actions over the threshold are blocked and escalated.

**Relevance to us: a new guardrail idea.** Our size-cap guardrail counts changed lines, not consequences. A Kubernetes blast-radius check is cheap to compute from manifests:
- **references:** which Deployments mount or reference the ConfigMap or Secret being changed (a shared ConfigMap affects every consumer);
- **redundancy:** does the target Deployment have more than one replica, and is a PodDisruptionBudget defined;
- **concurrency:** are other repair PRs open for the same namespace.

A fix that touches a resource used by many workloads could require a second approval or a note in the PR body. This also fits the Cloud-OpsBench idea of a dependency view as a missing tool.

#### D. Rollback mechanisms

**What it says.** Configuration snapshots before changes, restored atomically; previous state kept for stateful operations; automatic rollback if post-action verification fails or health degrades beyond thresholds, without a human.

**Relevance to us.**
- Configuration rollback is git itself.
- **Automatic rollback triggers are the gap named in §III.B.** Concretely, when verification fails after a merge, the operator could:
  1. open a revert PR of the merge commit;
  2. after it is merged, go back to analysis with the failed attempt in the feedback.
- Whether the revert PR needs human approval is a policy choice. Reverting to a known previous state is lower risk than a new fix, so it is a natural first candidate for auto-merge in the autonomy ladder.

### V. Progressive Autonomy

#### A. Trust levels (Table I)

**What it says.**

| Level | Name | Meaning |
| --- | --- | --- |
| 0 | Advisory | Agent suggests; human executes |
| 1 | Supervised | Agent executes with human pre-approval |
| 2 | Monitored | Agent executes autonomously; human reviews afterwards |
| 3 | Autonomous | No human involvement for approved categories |
| 4 | Self-improving | Agent can refine its own procedures based on outcomes |

**Relevance to us.**

| Their level | Our equivalent |
| --- | --- |
| 0 Advisory | Shadow mode (`REPAIR_SHADOW_MODE`): the proposal is recorded and the incident escalated, no PR. Blueprint L0/L1 |
| 1 Supervised | **Our default**: a PR that a human must merge. Blueprint L2 |
| 2 Monitored | Auto-merge with review afterwards. Optional R4 item |
| 3 Autonomous | Auto-merge with no review for proven categories |
| 4 Self-improving | Our recipe learning and the v4 crystallization. In the paper this is the top level; in our design, learning happens at level 1 already, because every learned recipe still goes through review |

That last point is a real difference. The paper ties self-improvement to the highest trust level. In our design, learning and autonomy are separate axes: the system can learn and get cheaper while a human still approves every change.

#### B. Promotion criteria

**What it says.** Authority is promoted on measured performance:
- success rate without human intervention;
- mean time to resolution;
- false-positive rate (unnecessary or wrong actions);
- rollback frequency;
- human override rate.

Promotion needs sustained performance above thresholds on all of them for a minimum evaluation period. No thresholds are given.

**Relevance to us.** These five metrics are a ready-made list for our event log and the thesis evaluation (R3, R4). We already record phase timestamps (time to resolution), outcome (success, escalation), and whether the merged file matches the proposal (a proxy for human override). Not yet recorded:
- false positives: a fix that was not needed;
- rollbacks: not implemented yet.

#### C. Demotion and circuit breakers

**What it says.**
- **Per-category breakers:** demote one incident category when its failure rate passes a threshold in a time window.
- **A global breaker:** fall back to advisory mode for everything if the overall failure rate spikes.
- **Override tracking:** patterns of human overrides trigger review and possible demotion.

**Relevance to us.**
- **Per category:** our recipe store demotes a promoted recipe on a failed outcome. That is per signature, which is finer than per category.
- **Global breaker:** a direct mapping. If, say, three verifications fail within an hour, the operator switches itself into shadow mode (advisory) and stops opening PRs until a human resets it. The switch already exists; only the trigger is missing.
- **Override tracking:** reviewer edits and rejected PRs are already visible to the operator (`reviewFeedback`, `mergedMatchesProposal`). Counting them per class is a small step.

### VI. Evaluation

**What it says.**
- **Deployment:** a production cloud network serving millions of customers (routers, switches, load balancers, firewalls) across distributed data centres.
- **Results**, after progressive deployment over several months:
  - autonomous resolution over 90% for well-understood failure modes;
  - resolution time down two orders of magnitude (hours to minutes);
  - false-positive remediation under 5% with no customer-visible impact;
  - on-call burden reduced;
  - knowledge preserved in playbooks;
  - more consistent quality.
- **Safety:** no critical incidents, rollbacks in a small percentage of executions (all within bounds), no action exceeding its predicted blast radius.
- **Figures 2–6:** radar charts and distributions; the text does not give their underlying numbers.

**Relevance to us.**
- **Be careful citing these numbers.** They come with no incident counts per category, no definition of "well-understood", no baseline method, no confidence intervals and no description of how the figures were produced. The Crystallization paper repeats the same three headline numbers from the same system. Use them as industry context ("a production system reports…"), not as a benchmark to compare against.
- **The evaluation dimensions are still useful** for our own evaluation design: resolution rate per category, time to resolution, false-positive rate, rollback rate, blast-radius compliance.

### VII. Lessons Learned

**What it says.**
- **A. LLM reliability:** LLMs are stochastic, which conflicts with safety-critical determinism. Mitigations:
  - structured output enforced by schema;
  - **multi-model consensus** for critical decisions;
  - deterministic safety checks on every LLM plan before execution.
- **B. Observability:**
  - decision tracing with full context;
  - causal links between observations, decisions and outcomes;
  - a human-readable explanation with each resolution.
- **C. Novel failures:**
  - confidence estimation with escalation below a threshold;
  - statistical novelty detection that brings in humans;
  - graceful degradation: when it cannot resolve, still give diagnostic enrichment and suggested actions.
- **D. Organisational change:**
  - trust is built through transparency and track record;
  - the engineer's role shifts to system improvement and novel problems;
  - clear accountability is needed for autonomous decisions.

**Relevance to us.**
- **Structured output and deterministic checks** are exactly our design: v2 uses a strict JSON schema, v3 the `propose_patch` schema, and both pass through the seven guardrails. This paper is good industry support for that choice.
- **Multi-model consensus fits our two providers.** v3 can already run both Groq (`gpt-oss-120b`) and Claude for the same incident. A consensus mode could run both and open a PR only when they agree on the file and the edit, escalating otherwise. It costs two investigations, which conflicts with the cost goals of v4, so it suits high-risk classes only. It would also be a measurable experiment: how often do the two models agree, and is agreement a good predictor of a correct fix?
- **Observability:**
  - decision tracing: our agent traces;
  - causal attribution: what the Cloud-OpsBench evidence graph makes measurable;
  - human-readable explanation: the PR body (root cause, rationale, diff).
- **Novel failures:**
  - **confidence:** v2 escalates below confidence 0.5;
  - **novelty:** in v4, "no known signature" is the novelty test that routes to the agent;
  - **graceful degradation:** when we escalate, the diagnosis should still reach the human (in the PR comment or incident status), not only the word "escalated".
- **Accountability:** for confidential pipelines the reviewer is accountable for every merge. That is one reason our default stays at "Supervised".

### VIII. Discussion

**What it says.**
- **Generalisability:** the patterns apply beyond networks (compute, storage, application services).
- **Limitations:**
  - only a subset of incident types is covered; long-tail and novel failures still need humans;
  - incidents spanning domains (network plus compute) need coordination between independent systems, still in development;
  - regulators' questions about accountability are not fully answered by technical means.
- **Future directions:**
  - cross-domain orchestration;
  - formal verification of agent safety properties;
  - federated learning to share knowledge across organisations without exposing proprietary details.

**Relevance to us.**
- **The multi-domain limitation is our multi-repo question.** One operator per domain or repository, coordinated, versus one agent that reads them all. Our plan-v3 notes already discuss the Argo CD Application as the way to find the right repo per workload.
- **Federated learning** is the same idea as "federated skills" (W8) in our MCP research notes. The confidential-computing angle adds something here: attestation is a natural way to decide whose shared recipes to trust.
- **Regulatory and accountability concerns** support keeping human approval for confidential or regulated workloads.

### IX. Conclusion

**What it says.** Multi-agent orchestration with strong safety and progressive autonomy can reach high autonomous resolution rates while keeping safety guarantees. The move to autonomous operations needs knowledge management, organisational change and trust building, not only technology.

**Relevance to us.** For the thesis: our contribution is not more autonomy. It is a safe, auditable, reversible repair path (GitOps plus review) for **confidential** pipelines, where the analysis itself must be protected. Neither this paper nor the Crystallization paper touches confidentiality.

---

## 4. What the paper does not give us

- **No method or numbers behind the results:** no incident counts, categories, baselines, confidence intervals, or data for the figures.
- **No promotion thresholds**, only the metrics.
- **No detail of the skills runtime.** "Inspired by" MCP; it does not say MCP is used.
- **No description of the LLMs**, prompts or planning method.
- **No confidential computing, attestation or protection of the diagnostic data.**
- **No GitOps.** Actions go directly to devices, with snapshot-based rollback.

## 5. What we take into the thesis

| Idea from the paper | Our step | Effort |
| --- | --- | --- |
| Automatic rollback on failed verification | Open a revert PR of the merge commit when verify fails, then re-analyse | Low–Medium |
| Global circuit breaker | Switch to shadow mode after N failed verifications in a window | Low |
| Concurrency limit | At most N open repair PRs per repo or namespace | Low |
| Blast-radius estimate | Guardrail: count workloads referencing the changed resource; check replicas and PodDisruptionBudget; note in the PR or require a second approval | Medium |
| Action classification | Classify PRs by risk (limits/tag vs Service vs trust config); stricter review for higher classes | Low |
| Plan success criteria | The proposal states what "fixed" means; the verifier checks it | Medium |
| Multi-model consensus | Run Groq and Claude for high-risk classes; open a PR only if they agree | Medium |
| Skill governance | Per-tool permission declaration, output validation and per-call timeouts in the MCP servers | Medium |
| Playbook schema | Recipes with preconditions, steps, decision points, verification criteria | Medium (v4) |
| Human edits as knowledge | Learn the reviewer's edited fix, not only the agent's proposal | Medium (v4) |
| Promotion metrics | Record success, time to resolution, false positives, rollbacks and overrides per class | Low |
| Graceful degradation | Always attach the diagnosis to an escalation | Low |

**Two axes of promotion.** This paper promotes *authority* per category: advisory → supervised → monitored → autonomous. The Crystallization paper promotes *execution type* per playbook: agent → hybrid → deterministic. Our thesis can keep them separate. A failure class can become cheap (deterministic recipe) while staying supervised (human merges). For confidential pipelines, that combination of low cost and full human control may be the right default.

Thesis research questions it touches:
- **R4 (repair and learning):** rollback, playbook schema, learning from human edits, promotion metrics.
- **R5 (safety):** layered authorization, blast radius, circuit breakers, consensus.
- **R3 (latency and cost):** consensus doubles cost; rollback adds latency.

## 6. Questions to raise with the supervisors

1. Should a revert of a failed fix be allowed to auto-merge (lower risk than a new fix), or still need review?
2. Is multi-model consensus worth its double cost for trust-relevant classes (CAS session, MRENCLAVE)?
3. Should the thesis treat authority and execution type as two separate axes explicitly, with our default fixed at "supervised"?

## References

- A. Malik, *Autonomous Incident Resolution at Hyperscale: An Agentic AI Architecture for Network Operations*, arXiv:2606.09122, 2026. [PDF](https://arxiv.org/pdf/2606.09122)
- Companion: A. Malik, *Progressive Crystallization*, arXiv:2607.07052, 2026, see [progressive-crystallization-overview.md](progressive-crystallization-overview.md).
- Related overview: [cloud-opsbench-overview.md](cloud-opsbench-overview.md).
- Our documents: [confidential-repair-loop-blueprint.md](../confidential-repair-loop-blueprint.md), [plan-v2-comparison.md](../plan-v2-comparison.md), [plan-v3/approach-c-and-hybrid.md](../plan-v3/approach-c-and-hybrid.md), [plan-v3/prototype-v3.md](../plan-v3/prototype-v3.md).
