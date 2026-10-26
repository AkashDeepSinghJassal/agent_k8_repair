# Approach C (MCP agent) and a hybrid with the operator

Date: 2026-10-26. Status: research notes, nothing here is built yet.

Today's prototype is **Approach A**: the operator collects a fixed evidence bundle, the LLM answers once (a template or small edits), seven guardrails check it, and a person merges the pull request. This note describes two alternatives and how each would reuse the prototype.

## 1. What is already known

| Source | What it shows | Use for us |
| --- | --- | --- |
| [Progressive Crystallization, arXiv 2607.07052](https://arxiv.org/pdf/2607.07052) | Three execution types: agent-orchestrated (10k–50k tokens, novel incidents), hybrid (1k–5k tokens, LLM only interprets), deterministic (0 tokens). A verified agent trace is turned into a template, then promoted; a circuit breaker demotes it on failure. One production system (cloud network operations) over 8 months: deterministic share rose to about 45%, per-incident agent cost fell over 70%. | The strongest match to our tiers and recipe store. One organisation, one domain, no controlled comparison. |
| [Kubernaut](https://pkg.go.dev/github.com/jordigilh/kubernaut) | Open-source Kubernetes remediation platform. An LLM agent investigates through the Kubernetes API, Prometheus and logs, picks a workflow from a catalog, runs it, and checks the result. Operators can join a session through MCP. | Closest existing operator-plus-agent system. Its page does not mention git or pull requests; it also has no confidential-computing angle. |
| [HolmesGPT, kagent, K8sGPT](https://www.cncf.io/blog/2026/01/07/holmesgpt-agentic-troubleshooting-built-for-the-cloud-native-era/) | Agent frameworks with Kubernetes toolsets and MCP support (from the earlier blueprint). | Off-the-shelf agent loop to compare against or host. |
| [kubernetes MCP server demo (sovereign-selfheal)](https://github.com/sovereign-selfheal/gitops/pull/40) | A read-only Kubernetes MCP server deployed by GitOps, with a service account bound to the built-in view role. | A pattern for the read-only tool layer. One small project, treat as a reference design. |
| [Graph Traversal Agent, arXiv 2606.08590](https://www.emergentmind.com/papers/2606.08590) | The model reasons over a typed evidence graph; deterministic operations collect and bound the evidence. Diagnosis only. | Shows how to bound an agent's search. |
| [MCP tool poisoning, CSA note](https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-tool-poisoning-auto-execution-20260701/) and MCPTox (36.5% average attack success over 20 models, as reported in a search summary) | Instructions hidden in tool metadata or tool output steer the model. | Our F8 fault (instruction injected into logs) becomes a real risk when log text drives tool choice. |
| [cMCP](https://mcpservers.org/servers/agentrust-io/cmcp) | Gateway that enforces tool-call policy inside a TEE and keeps a signed audit chain. Developer preview. | The only concrete "MCP inside a TEE" item found. No standards-based MCP attestation turned up. |

Caveat: these were found by web search and I read only two of them in detail (2607.07052 and the Kubernaut page). Verify the numbers in the full papers before they go into the thesis.

## 2. Approach C: an agent that gathers its own evidence over MCP

**Idea.** The detector still opens the incident. Instead of one fixed bundle and one answer, an agent loops: it asks for a log, an event list, a manifest or a git file through read-only tools, until it can call the single write tool, `propose_patch`.

```
incident → agent loop (plan → read-only tool call → observe, up to N steps)
         → propose_patch(diff) → seven guardrails → pull request → human → Argo CD → verify
```

**Tools (all read-only except the last):**

| Tool | Backed by |
| --- | --- |
| `k8s_get`, `k8s_events`, `k8s_logs` | Kubernetes API, namespace-scoped, secrets never returned |
| `argo_app_status` | Argo CD Application status |
| `git_read(path, ref)` | The repo, limited to `deploy/**` and `workflows/**` |
| `configmap_diff(name)` | The existing related-file evidence |
| `scone_*` (later) | CAS session, LAS and MRENCLAVE lookups (blueprint, Plan C) |
| `propose_patch(file, edits)` | The only write; always goes through the guardrails |

**Where it would fit in the code.** The analyzer already has one entry point, `analyze()`. An agent mode would be a second implementation behind the same interface (`REPAIR_ANALYZER_MODE=agent`), so detection, guardrails, pull request, verify and learning stay unchanged. This is why the comparison is cheap to run.

**Strengths.** It handles failures whose evidence is not in the fixed bundle (a Service selector typo, a second ConfigMap, a bad NetworkPolicy). The tools it chooses to call are themselves data for R1 ("which evidence does diagnosis need?"). The SCONE toolset would be a reusable contribution.

**Weaknesses.** More model calls, so more latency and cost (and, inside an enclave, more cost per call). Non-deterministic, so each fault needs repeated runs. The largest prompt-injection surface: a log line can now influence which tool is called next, not only the final answer.

**Needed controls.** Step and token budgets; an allowlist of tools and namespaces; every tool result passed through the existing redaction and treated as data, never instructions; no tool that writes to the cluster; the same seven guardrails on `propose_patch`; a full trace stored with the incident.

## 3. Hybrid: operator for control, agent for the hard cases

Keep the operator as the owner of state and safety. Let the agent be one more analyzer tier. Three variants, from smallest to largest change:

| Variant | How it works | What it tests |
| --- | --- | --- |
| **H1 Escalate to the agent** | Run today's one-shot analysis. If a guardrail rejects the result, the model reports low confidence, or the class is unknown, hand the incident to the agent loop. | Does the agent rescue what one-shot misses, and at what cost? |
| **H2 Agent as evidence collector** | The agent may only call read tools and returns an evidence bundle. The one-shot analyzer still decides, so the write path stays a single, schema-checked call. | Is the risk of agentic evidence gathering separable from agentic deciding? |
| **H3 Crystallization** | Verified agent traces become templates and recipes. A repeated failure is served by tier 0 (no tokens), a stable one by a scoped prompt. A failed verification demotes the recipe. | Does cost fall as the system learns? |

This maps directly onto the prototype's tiers:

| Crystallization paper | Our prototype |
| --- | --- |
| Type 3, agent-orchestrated | New agent tier (this note) |
| Type 2, hybrid | Tiers 2 and 3: the LLM picks a template or edits, code validates |
| Type 1, deterministic | Tier 0: promoted recipe, no LLM |
| Capture, promote, demote | Candidate → verified → promoted recipes, with demotion on a failed outcome (already built) |

**Recommendation.** Build H1 first, then H3. H1 is a small change (a second analyzer behind the same interface, triggered by a rejection), it gives the one-shot vs agent comparison, and it keeps the safe path as the default. H3 mostly extends the recipe store the prototype already has: the new part is capturing an agent's trace as a recipe. H2 is a good ablation if the injection results from C look bad.

## 4. How to compare A, C, H1, H3

- **Faults.** F1–F9 plus a few the fixed bundle cannot see (a Service selector typo, a second ConfigMap, a NetworkPolicy block).
- **Metrics per run.** Healed or not; wrong-but-harmless fixes (needs the semantic verify still on the roadmap); model calls, tokens and tool calls; time from event to pull request; guardrail rejections; injection success on F8.
- **Repeats.** At least three runs per fault per approach, and report the model used. The prototype's results so far are single runs on one model.
- **Questions each result answers.** R1: which tools the agent needed. R2: accuracy by class for A vs C. R3: latency and tokens by tier. R5: injection success when tool choice is model-driven.

## 5. What this means for the enclave work

An agent loop multiplies the number of model calls, so the cost per incident inside a SCONE enclave grows with tool calls. Tool servers become part of the trust question too: a swapped or poisoned tool can feed the agent false evidence even when the model is attested. cMCP is one existing design for policy enforcement inside a TEE, but it is a developer preview and I found no standard way to attest an MCP server.

## 6. Open questions

1. Build our own small agent loop, or host HolmesGPT or kagent as the Approach C baseline? Own loop gives control over budgets and traces; an existing framework is a fairer "standard tooling" baseline.
2. Which model for the agent loop? Groq's `gpt-oss-120b` is fast, which helps with many calls; the Claude run for comparison is still pending credits.
3. Does the thesis need all of A, C and the hybrid, or A plus H1 with C as the ablation? The second is the smaller scope.
4. Should the "approach D" CI agent (rules first, then a coding agent) stay in the comparison, or is it replaced by Kubernaut-style tooling as the external baseline?
