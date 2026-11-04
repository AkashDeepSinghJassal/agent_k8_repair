# Progressive Crystallization: paper overview and relevance to our thesis

**Paper:** Arun Malik, *Progressive Crystallization: Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production*, arXiv [2607.07052](https://arxiv.org/pdf/2607.07052) v1, 8 July 2026, cs.SE. Author affiliation: Microsoft Azure Networking. Short paper (about four pages, ten sections, three tables, three figures). A companion paper by the same author describes the production system it runs in: *Autonomous incident resolution at hyperscale*, arXiv 2606.09122.

**Our thesis:** Kubernetes-Native AI Repair Loop for Confidential GitOps Pipelines Using SCONE. We have a one-shot analyzer with tiers and a recipe store (prototype v2, branch `prototype`) and an MCP agent (prototype v3, branch `prototype-v3`). The planned hybrid (prototype v4, issue #22) is where this paper matters most.

**How this overview is built.** I read the full PDF. Every claim about the paper below comes from its text; numbers are the paper's own, from one production system. "Relevance" paragraphs are my analysis for our project.

---

## 1. The paper in one paragraph

LLM agents for IT operations cost the same every time they run, even for problems they have solved many times before. The paper treats the agent as a **discovery mechanism**, not the permanent execution engine. When the agent solves a problem, its trace is turned into a reusable playbook. With more successful runs, the playbook is **promoted** step by step from agent-driven (expensive, about 50% reproducible) to hybrid (the LLM only interprets) to deterministic code (zero tokens, 100% reproducible). A circuit breaker **demotes** a playbook when it fails. In one production system for cloud network operations, deterministic executions grew from 0% to about 45% in eight months, and per-incident agent cost fell by more than 70% while incident volume doubled.

## 2. Why it matters for us, in one paragraph

Our v3 agent is exactly the paper's starting point: it investigates every failure from scratch through MCP tools. One live Claude CLI run on F4 used about 61k input tokens for 9 tool calls (from the #17/#18 verification). Groq's free tier on this account allowed only a few such runs per day before hitting its 200k tokens/day limit. Inside a SCONE enclave each token will cost more. Our v2 already has the end states of the paper's ladder (tier 0 recipes, tiers 2–3 where code renders the LLM's choice). What we lack is the **bridge**: turning a verified v3 agent trace into a v2-style recipe, with evidence-based promotion and demotion. That bridge is the v4 design.

---

## 3. Section by section

### Abstract and I. Introduction

**What it says.** Agents observe state through tool calls, reason and act in a loop. That flexibility handles novel incidents but makes the agent "a permanent cost center": every run re-invokes full inference, the investigation path is non-deterministic and hard to reproduce, and cost grows linearly with incident volume. The core waste is that a successful investigation is thrown away, so the next occurrence rediscovers the fix "at the same token cost and with a possibly different and inferior result". Existing options do not close the gap: workflow engines cannot handle novel cases and need an engineer per automation; unconstrained agents never get cheaper; fine-tuning gives a smaller probabilistic model, not a deterministic workflow; recorded macros (RPA) capture surface actions, not reasoning or data flow, and break when the environment changes.

Four contributions: an execution-type taxonomy (§III), a promotion and demotion lifecycle (§IV), an economic model (§V), and a safety-monotonicity argument with production evidence (§VI–VIII).

**Relevance to us.**
- This is the problem statement for our v4. In v3 every F4 occurrence would re-run the full investigation (describe pod, read logs, read manifests, read git history, propose).
- "Possibly different and inferior result" is something we have already seen. For F10, the agent's fix pointed nginx at the renamed Service (`api-svc`), where the catalog's intended fix restores the Service name. Both heal; a fresh run might choose either. Crystallizing one verified answer makes the behaviour stable.
- The fine-tuning remark matches our blueprint's Approach E (a trained model) being a stretch goal rather than the main path: a tuned model is still probabilistic.

### II. Related Work

**What it says.** Builds on ReAct and Toolformer (agents that reason and act with tools) and on AIOps surveys. Prior work treats the agent as the permanent engine. FrugalGPT lowers the cost per call (cascades, routing) but stays probabilistic and never removes inference for solved problems. This paper is complementary: it removes inference for proven work by extracting deterministic workflows from execution traces. The extraction draws on **process mining** (van der Aalst), which recovers process models from event logs; here the logs are agent traces and the models are executable playbooks.

**Relevance to us.**
- Two different cost levers, and we can use both. *Routing* (FrugalGPT-style) is our H1 idea: try the cheap one-shot analyzer first, call the agent only when needed. *Elimination* (this paper) is our recipe tier: no model call at all for proven fixes.
- Process mining is the right vocabulary for the thesis when describing how we turn traces into recipes. Our agent traces (`<state_dir>/agent-traces/<incident>-a<attempt>.json`) are the event log.

### III. Execution-Type Taxonomy (Table I)

**What it says.** Three playbook types on one spectrum. They are lifecycle stages of the same behaviour, not separate systems.

| Type | How it runs | Determinism | Tokens per run |
| --- | --- | --- | --- |
| 3, agent-orchestrated | Sub-agents reason within a bounded scope; deterministic checkpoints; human approval for writes. Entry point for any novel incident | about 50% | high, 10k–50k |
| 2, hybrid | Fixed step structure; specific steps call the LLM only to interpret, classify or summarise; every action typed and schema-validated, so "the LLM decides understanding, not what to do" | about 90% | low, 1k–5k |
| 1, deterministic | Pre-coded logic, conditionals and typed API calls; no LLM at runtime | 100% | zero |

**Relevance to us: our prototypes already cover all three types, in different branches.**

| Paper type | Our equivalent | Where |
| --- | --- | --- |
| Type 3 | The MCP agent: read-only tools, `propose_patch`, human merges the PR | v3, `src/repair_agent`, `src/repair_mcp` |
| Type 2 | Tier 2: the LLM picks a template and parameters, code renders the edit. Tier 3: the LLM proposes a find/replace edit to one allowed file, checked by seven guardrails | v2, `src/repair_operator/analyzer` |
| Type 1 | Tier 0: a promoted recipe renders the fix with no LLM call. Tier 1: the rule table (rules-first mode) | v2, `knowledge.py`, `analyzer/rules.py` |

Two differences worth stating in the thesis:
- **Human approval.** The paper requires human approval for writes only at Type 3; Types 2 and 1 rely on schema validation and deterministic logic. Our design keeps a reviewed pull request at every type. That is safer and fits confidential pipelines, but it means our cost savings are in tokens and latency, not in human time. Auto-merge for proven recipes is listed as Optional under our R4.
- **The v2 tier 2 is a textbook Type 2.** "The LLM decides understanding, not what to do" is exactly why we made code render the template: the model chooses `raise_memory_limit`, code writes the YAML.

### IV. The Crystallization Lifecycle (Table II)

**What it says.** Four stages.

1. **Discovery.** A novel incident with no matching playbook runs as Type 3. Reads run autonomously, writes pause for approval. The complete trace is recorded.
2. **Capture.** On verified successful resolution, the trace is extracted into a reusable template: an ordered list of tool calls, the branch conditions the agent acted on, input and output schemas per step, a directed acyclic graph of tool dependencies, instance-specific values (device IDs, timestamps) turned into parameters, and human-approval points marked as explicit gates.
3. **Promotion to hybrid.** After repeated successful template runs, steps where the LLM always gives the same classification become deterministic rules; steps where reasoning varies but the outcome is stable become a scoped single-purpose prompt. Acceptance tests are generated from the successful traces, and the Type 2 candidate must pass them.
4. **Promotion to deterministic.** After more hybrid runs without LLM disagreement, the remaining LLM steps whose output comes from a finite set, or whose decision can be written as a rule, are replaced. The Type 1 playbook needs no tokens and is still validated by the stage 3 acceptance tests.

**Promotion criteria (Table II, defaults, configurable):**

| Transition | Requirements |
| --- | --- |
| Type 3 → 2 | ≥10 successful runs; zero safety violations; ≥90% of runs produce the same action sequence; all auto-generated acceptance tests pass; no human override in the recent window |
| Type 2 → 1 | ≥50 successful hybrid runs; LLM classification consistency ≥99%; the deterministic rule covers all observed input variation; full regression suite passes without the LLM; human review of the deterministic logic |

A key sentence: autonomy is attached to the playbook class and action type, based on its evidence, "rather than to the capability of the underlying model. A more capable model does not automatically earn more autonomy; a track record does."

**Relevance to us. This is the core of the v4 design.**

*Stage 1 (discovery)* is v3 as built. The trace we save already has the ordered tool calls with arguments and a result preview, plus the full evidence text. For F4 the live sequence was:

```
k8s_describe_pod → k8s_logs (previous) → git_read api.yaml → k8s_get configmap → git_list
→ git_read configmaps.yaml → git_log → git_diff → propose_patch
```

*Stage 2 (capture)* is what we must build. Applied to F4:
- *Parameterize:* namespace, pod name, container, ConfigMap name, the missing key. Our existing **knowledge signature** in `fingerprint.py` already strips namespace, pod and timestamps for cross-namespace learning; it is the natural playbook key.
- *Branch conditions:* "log says `X not set`" and "ConfigMap lacks key X" and "git history has a commit that removed X".
- *Dependency graph:* `git_diff` needs the file and commit from `git_log`, which needs the path from `git_read` of the Deployment.
- *Approval gate:* the pull request.

*Stage 3 (hybrid)* maps to v2 tiers 2/3: the investigation steps become fixed code (read pod, read referenced ConfigMaps, read git history of the file); the LLM is left only with "which removed value is the right one?". This is the paper's "scoped, single-purpose prompt".

*Stage 4 (deterministic)* is our tier-0 recipe: "restore the key removed in the last commit that touched this ConfigMap", rendered by code.

*Our current promotion rule is much weaker than Table II.* Ours: a recipe is promoted after 3 verified heals (demoed at 2), and a heal where the reviewer changed the proposal counts only as "approved", not toward promotion. That last rule is the same idea as the paper's "no human override". What we do not check: action-sequence consistency (≥90% same sequence), safety violations, generated acceptance tests, or 10+ runs. For the thesis this is a design choice to justify: with a fault catalog we can afford higher thresholds in experiments, and the consistency check is cheap to compute from traces.

*"Track record, not model capability"* is the same principle as our blueprint's autonomy ladder (L0 observe to L4, set per failure class in a `RepairPolicy`). The paper is a good citation for it.

### V. Economic Model (Fig. 2)

**What it says.** Type 3 costs about 10,000–50,000 tokens and seconds to minutes; Type 2 about 1,000–5,000 tokens; Type 1 zero tokens and milliseconds. What matters is not one run but the shift in the mix: requests are routed to the lowest-cost type available for their pattern, so the Type 1 share grows and total inference cost falls even as volume rises. The agent's cost is amortized over all future deterministic runs of what it discovered.

**Relevance to us.**
- Our measured v3 run sits at the top of the paper's Type 3 range: about 61k input and 1.9k output tokens, 9 tool calls, about 27 s, about $0.07 through the Claude CLI (one run on F4). This is one data point, not a measurement campaign.
- The Groq free tier on this account (200k tokens/day observed in a quota error) allows only a handful of agent runs per day. That is a concrete, practical reason for crystallization in our setup, not only a production-scale one.
- **SCONE makes the argument stronger.** In the enclave, inference is slower and more expensive per token. A Type 1 recipe needs no LLM inside the enclave at all, which also shrinks what has to be trusted at runtime.
- For R3 (latency and cost), the paper suggests the right metric: cost and latency **per execution type** plus the **mix over time**, not a single average.

### VI. Safety Monotonicity (Table III)

**What it says.** Making a workflow cheaper does not make it less safe; each promotion preserves or improves every tracked property.

| Property | Type 3 | Type 2 | Type 1 |
| --- | --- | --- | --- |
| Auditability | full trace | full trace | full trace |
| Reproducibility | about 50% | about 90% | 100% |
| Blast-radius control | human approval gates | schema validation | deterministic logic |
| Compliance | conditional (needs a human present) | built-in | built-in |

Blast-radius control moves from runtime checks to logic that "can be checked before execution rather than caught during it".

**Relevance to us.**
- This supports our R5 (safety) story: the deterministic recipe is easier to review, test and audit than the agent run it came from. In our design, every type also goes through the same seven guardrails and a reviewed pull request, so safety does not depend on the type at all; the promotion only improves reproducibility.
- **A caveat specific to us.** The paper's argument assumes that "successful" means correct. Our verification is health-only: pods become Ready and stay Ready for a window. F10 shows the gap: two different fixes (rename the Service back, or point nginx at the new name) both heal. A recipe promoted from health alone could freeze a fix that works but is not the one the team wants. Before relying on promotion, the thesis needs either semantic verification (an application-level probe, already on our roadmap) or human review of each recipe, which Table II already requires for the final Type 2 → 1 step.
- The paper's "blast-radius control" column is a good frame for our guardrails: path allowlist, size cap and policy rules are all checkable before anything runs.

### VII. Demotion and Continuous Discovery

**What it says.** Crystallization is not one-way. Each promoted playbook is monitored, and a circuit breaker demotes it on execution failure, safety violation or acceptance-test regression. Production example: a firmware update changed a command's output format; the deterministic parser failed; the playbook was demoted to hybrid so the LLM could handle the new format; after clean runs it was re-promoted. Novel incidents always enter as Type 3, so discovery never stops.

**Relevance to us.**
- Our recipe store already demotes a promoted recipe on a failed outcome. The paper adds the idea of demoting **one level** (to hybrid) rather than discarding the recipe.
- **We have hit the Kubernetes version of the firmware example.** Our F1 fault's `find` text expected `memory: 96Mi`, but a merged repair had left the live manifest at `memory: 24Mi`, so the exact-text edit no longer matched. A tier-0 recipe built on exact find/replace text breaks the same way whenever a file drifts. The paper's answer fits: demote to the hybrid tier, let the LLM adapt the edit to the current file, re-promote after clean runs.
- "Novel incidents always enter as Type 3" is the routing rule for v4: a failure with no matching signature goes to the agent; a known one goes to the cheapest promoted tier.

### VIII. Production Evaluation (Fig. 3)

**What it says.** A production agentic platform for cloud network operations, handling tens of thousands of incidents per month. Three observations:
1. **The mix shifts toward determinism.** At launch essentially all executions were Type 3. After eight months: about 45% Type 1, 30% Type 2, 25% Type 3. The Type 1:2:3 ratio is proposed as a platform-maturity metric.
2. **Cost falls as volume rises.** Per-incident agent cost fell more than 70% while incident volume roughly doubled.
3. **Autonomy and quality held.** Over 90% of common incident categories resolved autonomously, mean time to resolution from hours to minutes, false-positive remediation rate under 5% with no customer-visible impact.

**Relevance to us.**
- We cannot reproduce production scale, and we should not try to compare numbers directly.
- We **can** reproduce the *shape* of Fig. 3 in a controlled way: inject the same faults repeatedly (F1–F16, several runs each) and plot the share of runs served by agent, hybrid and recipe tiers over the sequence, with tokens and latency per run. That gives R3 and R4 a clear figure.
- The Type 1:2:3 ratio is a cheap metric to add to our event log (`metrics.py` already records the tier of each proposal).

### IX. Discussion, Limitations, Threats to Validity

**What it says.**
- One organization and domain; thresholds and ratios must be re-derived elsewhere (the lifecycle itself is domain-agnostic).
- It assumes recurring patterns; with mostly novel incidents most runs stay Type 3.
- Economic figures are platform-level observations, not a controlled comparison, over one eight-month window.
- Automatic promotion depends on the quality of acceptance tests generated from traces; an under-observed pattern can be promoted too early, which is why demotion and human review of the final logic exist.
- Extraction quality depends on trace richness; sparse or poorly structured logs limit what can be crystallized (a known issue in process mining).

**Relevance to us.**
- **The controlled comparison the paper lacks is something our thesis can provide.** Same faults, same cluster: one-shot (v2) vs agent (v3) vs hybrid with crystallization (v4), measured per run. That is a small, honest contribution on top of this paper.
- **Our faults recur by construction**, because we inject them. Results on a repeated catalog will flatter crystallization. The thesis should report repeated faults and novel ones separately; F10–F16 (cross-resource causes) are a start for the novel side.
- **Trace richness.** Our trace keeps a 500-character result preview per step plus the full evidence text. For extraction we will need full arguments and results per step, stored per tool call, not only a concatenated text.
- **Premature promotion** is real for us too (see the F10 caveat in §VI). The thesis should keep human review of every recipe before it runs without an LLM.

### X. Conclusion

**What it says.** Agent platforms do not have to be permanent cost centers. Treating exploration as discovery and crystallizing proven behaviour makes a system cheaper, faster and safer over time without humans rewriting the discovered patterns. The Type 1:2:3 ratio is proposed as a practical maturity metric, and promoting and demoting on evidence as a general pattern for affordable, trustworthy autonomy.

**Relevance to us.** It is the clearest published statement of the direction our prototypes already point in: v3 discovers, v2's tiers execute cheaply, v4 should connect them.

---

## 4. What the paper does not give us

- **No extraction algorithm in detail.** Stage 2 lists what is extracted (tool sequence, branch conditions, schemas, dependency DAG, parameters, gates) but not how, for example how branch conditions are detected from a trace. We will need to design this ourselves.
- **No method for generating acceptance tests** from traces, although promotion depends on them.
- **No per-type accuracy numbers**, only platform totals.
- **No baseline or ablation.** The 70% cost reduction is observational.
- **Thresholds are defaults**, explicitly to be re-derived per domain.
- **Writes without human approval at Types 1 and 2**, which is a different risk posture from ours (reviewed pull requests always).
- It is a short paper; the companion paper (arXiv 2606.09122) describes the system's architecture but not the extraction algorithm either, see [hyperscale-incident-resolution-overview.md](hyperscale-incident-resolution-overview.md).

## 5. What we take into the design (prototype v4)

| From the paper | Our v4 step | Status today |
| --- | --- | --- |
| Type 3 discovery with full traces | v3 agent; store full per-step arguments and results | Built, traces need more detail |
| Capture a verified trace as a template | Extract tool sequence, parameters (via the knowledge signature) and the final edit shape from a Healed incident | Not built |
| Type 2 hybrid: fixed steps, scoped prompt | Fixed evidence steps learned from the trace, one narrow LLM question; reuse v2 tiers 2/3 and guardrails | v2 tiers exist, not linked to traces |
| Type 1 deterministic | v2 tier-0 recipe | Built |
| Table II criteria | Raise our promotion rule: run count, action-sequence consistency, no reviewer edits, zero guardrail violations | Partly (reviewer-edit rule exists) |
| Circuit breaker, demote one level | Demote a failing recipe to hybrid instead of deleting it | Demotion exists, one-level demotion not |
| Route to the cheapest available type | H1: recipe if signature promoted, else hybrid if template exists, else agent | Not built |
| Type 1:2:3 ratio | Log execution type per incident; plot over repeated fault runs | Tier is logged; no plot |

Thesis research questions it touches:
- **R3 (latency and cost):** per-type tokens and latency, and the mix over repeated runs.
- **R4 (repair and learning):** promotion and demotion on evidence, human override as a signal.
- **R5 (safety):** safety monotonicity, with our caveat that health-only verification can promote a fix that works but is not the intended one.

## 6. Questions to raise with the supervisors

1. Should promotion in the thesis ever lead to a write without a reviewed pull request (the paper's Type 1), or do we keep human review at every type for confidential pipelines?
2. Is a controlled comparison on injected faults (v2 vs v3 vs v4) an acceptable evaluation, given the paper's own evaluation is observational?
3. Which promotion thresholds are realistic for a thesis-sized experiment, given that Table II's 10 and 50 runs per pattern multiply quickly across 16 faults?

## References

- A. Malik, *Progressive Crystallization: Turning Agent Exploration into Deterministic, Lower-Cost Workflows in Production*, arXiv:2607.07052, 2026. [PDF](https://arxiv.org/pdf/2607.07052)
- A. Malik, *Autonomous incident resolution at hyperscale: An agentic AI architecture for network operations*, arXiv:2606.09122, 2026, see [hyperscale-incident-resolution-overview.md](hyperscale-incident-resolution-overview.md).
- Our documents: [plan-v3/approach-c-and-hybrid.md](../plan-v3/approach-c-and-hybrid.md), [plan-v3/prototype-v3.md](../plan-v3/prototype-v3.md), [plan-v3/prototype-v3-results.md](../plan-v3/prototype-v3-results.md), [confidential-repair-loop-blueprint.md](../confidential-repair-loop-blueprint.md).
