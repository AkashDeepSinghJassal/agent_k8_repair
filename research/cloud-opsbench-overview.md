# Cloud-OpsBench: paper overview and relevance to our thesis

**Paper:** Yilun Wang, Guangba Yu, Haiyu Huang, Yujie Huang, Zirui Wang, Pengfei Chen, Michael R. Lyu, *Cloud-OpsBench: A Reproducible Benchmark for Agentic Root Cause Analysis in Cloud Systems*, arXiv [2603.00468](https://arxiv.org/pdf/2603.00468) **v2, 22 August 2026**, cs.SE. The Chinese University of Hong Kong and Sun Yat-sen University. 14 pages. Code and data: https://github.com/LLM4Ops/Cloud-OpsBench.

**Version note.** Version 2 has **754 cases across 57 fault types**. Our earlier notes ([plan-v3/mcp-from-scratch.md](../plan-v3/mcp-from-scratch.md), [plan-v3/failure-taxonomy.md](../plan-v3/failure-taxonomy.md)) quote 452 cases and 40 root-cause types; those come from version 1.

**Our thesis:** Kubernetes-Native AI Repair Loop for Confidential GitOps Pipelines Using SCONE. Prototype v2 (one-shot analyzer, branch `prototype`) and v3 (MCP agent, branch `prototype-v3`) both diagnose and **repair** failures through reviewed pull requests; v4 will be the hybrid.

**How this overview is built.** I read the full PDF. Numbers about the paper come from its text and tables. The "Relevance" paragraphs are my analysis for our project.

---

## 1. The paper in one paragraph

LLM agents now investigate cloud failures by choosing tools, reading the results and revising hypotheses. Existing benchmarks cannot evaluate this well. Static datasets give fixed logs, so the agent never chooses what to look at. Live testbeds allow real tool use, but the same injected fault looks different on every run, so two agents are never compared on the same incident. Both kinds score only the final answer. Cloud-OpsBench injects each fault in a real Kubernetes cluster, verifies it, and **freezes the cluster's observable state as a snapshot**. Agents then investigate through 12 standard diagnostic tools that **replay** answers from the snapshot, so every agent faces the identical incident. Each case has a ground-truth root cause and a **diagnostic evidence graph**: the facts an investigation must establish to justify that root cause. Across ten LLM agents, the best final-answer accuracy is 0.76 (OnlineBoutique) and 0.68 (TrainTicket), but full evidence closure for those agents is only 0.38 and 0.15. Agents often get the right answer without the evidence that supports it.

## 2. Why it matters for us, in one paragraph

It matters for three reasons.

- **A method for evaluating diagnosis.** Our evaluation so far is single live runs, so every run sees a slightly different incident: timing, Groq quota, a rollout keeping the old pod. The snapshot-and-replay method shows how to compare v2, v3, v4 and the two LLM providers on identical evidence. Our MCP servers are already the single interface the agent uses, which makes replay at that layer natural.
- **Process metrics.** Its process metrics measure what our health-only verification cannot: whether a fix was proposed for the right, evidenced reason. Our own F10 run is an example of the gap.
- **A map of the fault space.** Its 57-type taxonomy shows where our 16 faults sit, and what we do not cover.

What the paper does **not** do is repair: it scopes out remediation. Our thesis covers detection, diagnosis, repair through GitOps and verification. The two fit together: Cloud-OpsBench for the diagnosis half, our loop for the rest.

---

## 3. Section by section

### Abstract and 1. Introduction

**What it says.** Root-cause analysis (RCA) in cloud-native systems is a debugging task across application code, deployment specs, runtime configuration, service dependencies and infrastructure. LLM agents make it interactive. Three gaps in current benchmarks: no system-facing interaction in static ones, run-to-run variability in live ones, and outcome-only scoring in both. Contributions:
1. a runtime-verified benchmark of 754 cases and 57 fault types on two microservice workloads, built with a knowledge-guided pipeline;
2. decoupled fault realization (live) and evaluation (replay from snapshots);
3. process-grounded evaluation with milestone-based evidence graphs;
4. experiments with ten LLM agents.

Headline findings:
- the outcome–process gap;
- reusing past experience helps (trajectory examples raise DeepSeek-V4-Flash from 0.76 to 0.83 JRA and 0.38 to 0.57 ECR on OnlineBoutique);
- failures cluster into four categories.

**Relevance to us.**
- The paper's framing, "a conclusion must be supported by acquired observations", is the same principle as our `grounded_values` guardrail: a value the fix introduces must appear in the evidence. They apply it to diagnosis scoring; we apply it as a gate before a pull request exists.
- For us the gap matters more than for pure RCA. An ungrounded diagnosis in an RCA tool is a wrong report; in our loop it becomes a pull request that changes production config.

### 2. Motivation (2.1 Agentic RCA, 2.2 Evaluation Gaps, 2.3 Requirements, Table 1, Fig. 1)

**What it says.**
- **2.1:** in Kubernetes, behaviour comes from code, specs, configuration, dependencies and infrastructure together, so a symptom at an entry service can start several layers away. Two agents can reach the same root-cause label with very different evidence behind it. Operational knowledge (runbooks, API semantics) can be given to the agent, but any accepted diagnosis must still rest on case-specific observations.
- **Gap 1, static benchmarks:** they evaluate reasoning over supplied evidence, not the ability to acquire it. Example: in a TrainTicket read-only database case, the real skill is going from "payment fails" to querying ts-payment-service's dependencies, picking ts-order-service, and reading its logs.
- **Gap 2, live benchmarks:** the same injection varies in severity, duration and scope with concurrent load, scheduling and retries, so differences between agents cannot be attributed to the agents alone. Each run also costs a full cycle (clean state, warm up, inject, verify, investigate, restore), which multiplies with cases × agents × configurations.
- **Gap 3, outcome-only metrics:** a correct guess and a well-evidenced diagnosis score the same; a well-grounded investigation with a wrong final label scores the same as no investigation.
- **Requirements:**
  - R1, system-facing evidence acquisition;
  - R2, matched, reusable incident evidence;
  - R3, process evaluation that does not prescribe one command sequence.
- **Scope:** interactive investigation over preserved evidence, explicitly *not* live remediation.
- **Table 1** compares LogHub, RCAEval, TimeSeriesBench, Nezha, OpenRCA (static), AIOpsArena, AIOpsLab, ITBench (live) and Cloud-OpsBench (state snapshot). Only Cloud-OpsBench ticks all three gaps.

**Relevance to us.**
- Gap 1 is exactly the difference between our v2 and v3. v2's one-shot analyzer is a "static" setting: the operator collects a fixed bundle and the LLM reasons over it. v3's MCP agent chooses what to read. The paper gives a citable argument for why v3 is a different, harder capability.
- Gap 2 is our current evaluation problem. Examples from our own runs:
  - F13 takes about 7 minutes to reach CrashLoopBackOff;
  - F10 needs a pod restart before it shows;
  - a stuck rollout keeps the old pod Ready;
  - the Groq free-tier quota ended runs early.

  Repeating live runs gives averages, not matched comparisons.
- Gap 2's cost argument applies to us at laptop scale: every live run of our loop needs a fault injection commit, an Argo CD sync, an investigation, a pull request, a human merge, a second sync and a verify window, then a revert.
- Their explicit "no remediation" scope is the opening for our thesis: we cover what happens *after* diagnosis.

### 3. Cloud-OpsBench (3.1 Design and task formulation)

**What it says.** The **state snapshot paradigm**:
1. Each fault is realized in a real cluster.
2. After a Verifier confirms the symptom, the tool-observable context is captured: resources, configuration, topology and component states after verification, plus timestamped events, logs and metrics from the incident interval (from the pre-injection baseline to manifestation).
3. During evaluation the agent talks to a mocked interface that replays answers from the snapshot (for example `kubectl get pods`).

This does not remove noise from the original incident; it removes *evaluation-time* nondeterminism.

The task is formalized as an episode. The agent gets only a user query `U` describing the symptom, never the snapshot itself. It issues tool calls `t_i` and receives observations `o_i`, producing a trajectory `T = [(t1, o1), …, (tn, on)]`. It ends with a diagnosis `R̂ = ⟨component, fault type⟩`. `R̂` is scored against the ground truth `R*`, and `T` against the evidence graph `D*`.

**Relevance to us.**
- Their trajectory `T` is the same structure as our agent trace (`<state_dir>/agent-traces/<incident>-a<attempt>.json`: step, tool, args, result preview). We can score our traces the same way once we have evidence graphs (see 3.3.3).
- Their user query `U` corresponds to our **seed**: incident, namespace, pod, owner, container, reason, class hint. Our seed gives the agent more than theirs (the operator's class hint and the manifest path). For a fair comparison with their numbers, the seed would need to be reduced to the symptom only.
- **The replay idea maps directly onto MCP.** Our agent reaches the cluster only through the `k8s`, `git` and `argo` MCP servers. A "record" mode could save every request and response during a live run; a "replay" mode could serve them back. Different agents and providers would then diagnose the identical incident. This is a cheap, concrete evaluation feature for v3/v4.

### 3.2 Phase 1: Fault Knowledge Base Construction (Table 2)

**What it says.** Fault types come from three sources: the Kubernetes documentation (specification misconfigurations such as wrong Service selectors), developer communities (Stack Overflow, KodeKloud) and research literature.
- **Extraction:** Gemini 3 Pro turns each source into a structured entry with few-shot prompting. Each entry has metadata (name, causal mechanism, expected local failure, possible upper-layer symptoms) and a parameterized reproduction template with typed variables instead of concrete resource names.
- **Checks:** an LLM consistency check, then review by people with cloud-operations experience.
- **Admission:** a fault type is kept only if at least one instantiated case passes runtime validation.
- **Result:** 57 types in 8 categories, each with a difficulty label.

| Category | Mechanism | Difficulty | Cases | Example types |
| --- | --- | --- | --- | --- |
| Admission Control | API server rejects requests (quota, permission) | Medium | 58 | namespace CPU/memory/pod quota exceeded, missing ServiceAccount |
| Scheduling | Pods Pending on unsatisfied constraints | Easy | 164 | node selector/affinity mismatch, taint/toleration, CPU/memory capacity, PVC storage class/capacity/access mode |
| Startup | Pod creation fails | Easy | 86 | volume mount permission, missing Secret, wrong image reference, registry DNS failure, missing image pull secret |
| Runtime | Crashes or failing probes | Easy | 141 | OOMKilled, liveness/readiness probe wrong port/protocol/timing, MySQL bad credentials/port, DB read-only, connection exhaustion, zero replicas |
| Service Routing | Traffic between components fails | Medium | 91 | Service selector, port mapping, protocol mismatch, env var address mismatch, gateway misroute, DNS resolution failure |
| Performance | Saturation or network loss | Hard | 76 | CPU overload, pod/node network delay, packet loss |
| Infrastructure | Control-plane or node component outage | Medium | 40 | containerd, kubelet, kube-proxy, scheduler unavailable |
| Application Code Defect | Bugs in business logic | Hard | 98 | missing parameter, busy loop, wrong return, memory leak, artificial delay |

**Relevance to us: where our 16 faults sit.**

| Their category | Our faults | Comment |
| --- | --- | --- |
| Scheduling | F7 (unschedulable), F14 (memory request too large), F15 (PVC with missing StorageClass) | F15 is their `PVCStorageClassMismatch`; F14 is `MemoryCapacityMismatch` |
| Startup | F2 (bad image tag), F3 (missing ConfigMap), F12 (ConfigMap renamed in demo-b), F16 (runAsNonRoot) | F2 ≈ `IncorrectImageReference` |
| Runtime | F1 (OOM), F4 and F8 (missing env value), F5 (wrong command), F11 (subPath key), F13 (liveness probe port) | F13 = `LivenessProbeIncorrectPort` |
| Service Routing | F10 (api Service renamed, nginx cannot resolve `api`) | ≈ `ServiceDNSResolutionFailure` / `ServiceEnvVarAddressMismatch` |
| Admission, Performance, Infrastructure, Code defect | none | our detectors only react to crashing, failing-to-start or unschedulable pods and failed workflows |
| *not in their taxonomy* | F6 (failed Argo Workflow step), F9 (repeat in another namespace) | CI/CD pipeline failures and repeat failures are our own dimensions |
| *not in their taxonomy* | SCONE, CAS and attestation failures (planned) | the confidential-computing classes are our thesis contribution |

Also useful:
- **Fix location.** Their taxonomy has no "where does the fix live" column. For a GitOps repair loop that is the key question: most Scheduling, Startup, Runtime and Routing faults are fixable in git, while Infrastructure and most Performance faults are not and should escalate.
- **Generating faults.** Their LLM-plus-review pipeline is a model for growing our fault catalog systematically instead of by hand. Our fault subagent did a manual version: research, candidate list, live check, rejection of faults that crash nothing.

### 3.3 Phase 2: Automated Fault Case Generation (3.3.1 Injection loop, 3.3.2 Snapshots, Fig. 2)

**What it says.**
- **Testbed:** Kubernetes 1.31, four nodes on Huawei Cloud ECS, with Prometheus and Istio for metrics and the native API for state and logs. Workloads are OnlineBoutique (11 microservices) and TrainTicket (41, with databases), with Locust traffic. Injection uses ChaosBlade for infrastructure perturbations, `kubectl` for configuration mutations, and mutated service images for code defects.
- **Three-agent loop:**
  - A **Generator** (Gemini 3 Pro) writes a case-specific fault plan: prerequisites, selectors, injection steps and validation assertions.
  - An **Executor** restores a clean deployment, resolves selectors, checks a healthy baseline (including one successful end-to-end request), then injects.
  - A **Verifier** accepts only when the fault condition is active, the expected local failure is present and any upper-layer symptom is seen. This matters because "Kubernetes recovery mechanisms may mask an injected fault", for example a Pod rescheduled away from a tainted node. On failure, structured feedback goes back to the Generator. Up to three correction attempts; 800 plans generated, 46 rejected.
- **Snapshots:** the 12 tools are pre-executed over every case-valid argument, and the responses are stored as a JSON repository. On average 599 responses (3.46 MiB) per OnlineBoutique case and 1,708 (8.75 MiB) per TrainTicket case.
  - Requests are validated and canonicalized; a missing object returns the captured "Not Found"; a valid request outside coverage returns `UnsupportedQuery`.
  - Four evidence channels: resources and events; topology and telemetry; cluster context; source code.

**The 12 tools (Table 3):**

| # | Tool | Our nearest MCP tool |
| --- | --- | --- |
| T1 | GetResources(type, namespace, name, flags) | `k8s_list`, `k8s_get` |
| T2 | DescribeResource(type, name, namespace) | `k8s_describe_pod`, `k8s_events` |
| T3 | GetAppYAML(service_name) | `git_read` (the desired state in git, not the live object) |
| T4 | GetServiceDependencies(service_name) | none |
| T5 | GetRecentLogs(service_name, namespace) | `k8s_logs` |
| T6 | CheckConnectivity(namespace, svc, port) | none |
| T7 | GetClusterConfiguration() (nodes, taints, resources) | none (our allowlist excludes cluster-scoped objects) |
| T8 | GetAlerts() | none (no metrics) |
| T9 | GetErrorLogs(service_name, namespace) | `k8s_logs` (no error filter) |
| T10 | CheckNodeServiceStatus(node, component) | none |
| T11 | ListCodeFiles(app) | `git_list` (manifests only, not app code) |
| T12 | GetSourceCode(app, file) | `git_read` (manifests only) |
| none | | `git_log`, `git_diff` (change history) |
| none | | `argo_app_for_namespace`, `argo_app_status`, `workflow_status` |

**Relevance to us.**
- **Their Verifier solves a problem we keep hitting by hand.** "Kubernetes recovery may mask a fault" is exactly what we saw:
  - F4, F8, F10, F11 and F12 only show after a pod restart;
  - a stuck rollout keeps the old pod Ready;
  - F13 takes minutes to reach CrashLoopBackOff.

  An automated "inject, then verify the symptom, then hand to the operator" step would make our live runs repeatable. The dashboard's restart shortcut is a manual version of that.
- **Our tool set is narrower in observability and wider in change history.** We have no dependency graph, connectivity probe, node and cluster view, alerts or metrics, so our agent cannot diagnose Performance or Infrastructure faults, and it struggles with Scheduling faults that need node data (F14). But we have something they do not: **git history and Argo CD state**. For GitOps-managed failures the culprit commit is often the most direct evidence. The same group's earlier ChangeRCA paper (their reference 51) is about finding root causes from software changes; it is worth reading.
- **Candidate tools to add** to our MCP servers, in order of fit:
  1. `k8s_nodes` (read-only node capacity and taints), for Scheduling faults;
  2. a dependency view built from Services and their selectors, which would have helped on F10;
  3. `check_connectivity` (an in-cluster TCP probe, read-only), for Routing faults.
- **Replay sizing:** about 600–1,700 recorded responses per case at a few MiB is small. Recording our MCP calls during live runs is cheap.
- **Testbed contrast.** Their testbed (four cloud nodes, OnlineBoutique with 11 services, TrainTicket with 41) is far beyond our laptop: one kind node and about 3.8 GB of Docker memory. Running their workloads live is not realistic for us; using their released snapshots is (see section 5).

### 3.3.3 Ground-Truth Annotation (evidence graphs)

**What it says.** Each case has an outcome label `R* = ⟨faulty component, fault type⟩` and a process label: the **diagnostic evidence graph** `D* = ⟨M, K, ≺, Q⟩`.
- **M, milestones:** atomic diagnostic facts, not tool actions. "The PVC is Pending" is a milestone; "inspect the PVC" is not.
- **K, groups:** milestones are grouped. A singleton group is a mandatory fact; a multi-milestone group holds interchangeable facts that make the same advance.
- **≺, prerequisites:** a directed acyclic relation between groups that fixes the required order only where one fact depends on another.
- **Q, evidence bundles:** for each milestone, the admissible ways to establish it, each a set of clauses (tool, allowed arguments, a predicate over the response). A milestone is established when any one bundle is fully observed.

So the graph accepts alternative facts, alternative evidence sources and any independent order, without a canonical command sequence.

Process:
1. Three annotators, each with at least three years of cloud fault diagnosis experience, start from the causal backbone found during verification and keep only atomic, snapshot-observable facts.
2. Every annotation is replayed automatically against its snapshot.
3. Two blinded auditors review it, with a third adjudicating.

9.4% of graphs were revised. On average a graph has 3.3 milestones and 2.7 admissible bundles per milestone.

**Relevance to us: the most directly reusable idea.**

We can write a small evidence graph for each of our 16 faults by hand (3–4 milestones each, as in the paper). Example for F4 (missing `DB_URL`):

```
U: api pod in CrashLoopBackOff
 → M1  container exits with "DB_URL not set"            (k8s_logs, previous=true)
 → M2  api-config ConfigMap has no DB_URL key            (k8s_get configmap  ∨  git_read configmaps.yaml)
 → M3  a commit removed DB_URL, old value redis://...    (git_log + git_diff on configmaps.yaml)
 → R*: component api-config, fault CONFIG_ERROR (missing key); fix = restore key from M3
```

Our live F4 trace covered all three milestones in order (`k8s_logs` → `k8s_get` → `git_log` → `git_diff`), so the evidence graph would be fully closed.

**F10 shows the outcome–process gap in our own data.** The intended root cause is "the api Service was renamed to `api-svc` in a recent commit". The agent's trace listed Services and read the nginx config, but never called `git_log` or `git_diff` on `api.yaml`. It proposed pointing nginx at `api-svc`. That fix heals (the outcome check passes), but the milestone "a commit renamed the Service" was never established. Under this paper's metrics the run would get credit for the outcome but would not close the evidence graph.

This matters for repair more than for RCA:
- a fix chosen without the full causal chain can be the wrong fix even when it works;
- if that run is promoted into a recipe (v4), the wrong-but-working fix becomes permanent.

Possible uses:
- **Evaluation:** compute milestone coverage and evidence closure for every v3/v4 run from the traces we already save.
- **A new guardrail (process gate):** before a proposal for a known fault class becomes a pull request, require that the class's mandatory milestones were observed in the trace. A softer version would lower confidence and add a reviewer note in the PR body when a milestone is missing.
- **Promotion gate for v4:** only crystallize a recipe from runs that closed the evidence graph, not just from runs that healed.

### 4. Evaluation (4.1 Setup and metrics, Table 4)

**What it says.**
- **Research questions:** RQ1 outcome effectiveness, RQ2 evidence process, RQ3 experience reuse, plus a failure-mode analysis.
- **Agent:** a standard ReAct agent with Pydantic-validated tool parameters, temperature 0, a cap of 20 steps, extended thinking off.
- **Ten models:** GPT-5, Claude-Sonnet-4, Gemini-2.5-Pro, Qwen3.5-Plus, Gemini-2.5-Flash (proprietary); DeepSeek-V4-Flash, Qwen3-235B-A22B, Qwen3.5-27B, Qwen3-14B, Qwen3-8B (open-weight).

**Metrics:**

| Metric | Meaning |
| --- | --- |
| CA, FA | component accuracy, fault-type accuracy |
| **JRA** | joint RCA accuracy: both right |
| MC | milestone coverage: fraction of milestone groups established |
| EOC | evidence-order consistency: groups established after their prerequisites |
| **ECR** | evidence closure rate: 1 only if every required group is established |
| EE | evidence efficiency: fraction of tool calls that contribute to required evidence |
| Steps | number of tool calls |
| RAR | redundant action rate: repeats of an earlier tool and argument pair that add nothing |

Selected OnlineBoutique rows from Table 4:

| Model | CA | FA | JRA | MC | EOC | ECR | EE | Steps | RAR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| DeepSeek-V4-Flash | 0.88 | 0.77 | **0.76** | 0.74 | 0.64 | 0.38 | 0.62 | 6.45 | 0.00 |
| GPT-5 | 0.86 | 0.69 | 0.68 | 0.64 | 0.55 | 0.21 | 0.64 | 5.56 | 0.00 |
| Claude-Sonnet-4 | 0.71 | 0.56 | 0.52 | 0.61 | 0.54 | 0.44 | 0.44 | 6.04 | 0.00 |
| Qwen3.5-27B | 0.85 | 0.71 | 0.70 | 0.75 | 0.70 | 0.43 | 0.54 | 6.87 | 0.01 |
| Qwen3-8B | 0.35 | 0.19 | 0.19 | 0.58 | 0.40 | 0.16 | 0.37 | 16.01 | 0.46 |

**Relevance to us.**
- **These metrics are directly computable from our traces.** The trace has the tool, the arguments and a result preview per step. With per-fault evidence graphs, MC, EOC, ECR, EE, Steps and RAR follow mechanically. JRA maps to "proposal class and file match the catalog's `expected_class` and `correct_fix`".
- **RAR is something we already met.** During v3 development the Groq loop re-read the same files repeatedly until a timeout. We added a rule that a third identical tool call returns an error telling the model to finish. That is an RAR control. The paper shows the problem is concentrated in weaker models (Qwen3-8B RAR 0.46).
- **Step budget.** Their cap is 20 steps, ours is 30 tool calls. Our live runs used 9–11 calls, close to their strong models (about 6–7).
- **Model choice.** Our v3 hosts are Claude Sonnet through the Claude CLI and `gpt-oss-120b` through Groq. Neither is in their model list, so we cannot borrow their numbers for our models.

### 4.2 RQ1: Outcome Effectiveness (Fig. 3)

**What it says.**
- **Component vs mechanism:** agents often find the right component but not the mechanism (CA above FA). For example, they confuse image-reference errors with registry failures, or Service selector, port and protocol misconfigurations.
- **Open-weight models are competitive:** DeepSeek-V4-Flash has the best outcome scores on OnlineBoutique, and Qwen3.5-27B is close to GPT-5. The paper notes that RCA reads sensitive operational artifacts, so open-weight models matter when "cost, latency, or data-governance constraints" apply.
- **Difficulty:** JRA falls with difficulty, averaged across models:
  - OnlineBoutique 0.73 (Easy) → 0.51 (Medium) → 0.30 (Hard);
  - TrainTicket 0.67 → 0.43 → 0.08.

  Easy faults show direct Kubernetes symptoms; Medium faults need correlating several objects; Hard faults give indirect signals.

**Relevance to us.**
- **Confidential computing.** The open-weight result is relevant to the SCONE part of the thesis. An analyzer inside an enclave cannot call a remote proprietary API with confidential evidence. The paper's data suggests a mid-size open model run locally can be competitive on Kubernetes RCA. That supports the plan to run the analyzer inside an enclave instead of calling an external API.
- **Difficulty as a reporting dimension.** Most of our faults are "Easy" in their terms; F10, F11 and F12 are "Medium" (cross-object). This matches our reason for adding them: they are the cases where the operator's fixed evidence bundle is not enough. Reporting our results split by difficulty would make them comparable in spirit with the paper.
- **Component vs mechanism.** The CA/FA split fits our case: our proposal names both the file (component) and the class (mechanism), and they can be scored separately.

### 4.3 RQ2: Process Alignment

**What it says.**
- **Outcome overstates grounding:** GPT-5 0.68 JRA but 0.21 ECR on OnlineBoutique, and 0.68 vs 0.15 on TrainTicket. Agents infer the right answer from strong local signals without the full chain: "ungrounded diagnosis".
- **Broad but shallow:** moderate to high MC but low ECR (Gemini-2.5-Pro 0.74 MC vs 0.46 ECR). EOC is a little below MC, so some evidence is seen before its prerequisite: the search is partly opportunistic.
- **Separable capabilities:** getting the answer, grounding it, and doing so efficiently are different abilities. GPT-5 and Qwen3.5-Plus reach about 0.65 EE in about 6 steps; Qwen3-8B uses 16 steps at 0.37 EE.
- **Redundancy:** redundant calls appear mainly in weaker models; strong models have RAR near zero.

**Relevance to us.**
- For SREs, the paper says, "a correct RCA label is not enough; the diagnosis should also show which evidence supports the intervention." In our loop the reviewer is that SRE. The pull request body is where the evidence chain should appear. Today it carries the root cause, rationale and the diff. Adding the trace's key observations (the log line, the commit that removed the value) would make review faster and more trustworthy.
- "Broad but shallow" warns against equating more tool calls with better diagnosis when we compare v3 runs.

### 4.4 RQ3: Experience-Augmented Agents (Table 5)

**What it says.** For each root cause, cases are split about 1:1 into training and test sets. An experience pool is built only from GPT-5's successful training trajectories. Two methods:
- **Trajectory-as-ICL:** retrieve three successful trajectories matched by the reported symptom and put them in the prompt.
- **SOP:** distil recurring diagnostic actions and branches into category-level runbook guidance.

Neither uses the target root-cause label for retrieval.

Results (averages over Gemini-2.5-Flash, Qwen3.5-27B and DeepSeek-V4-Flash):
- **OnlineBoutique:** JRA 0.70 → 0.79 (ICL) / 0.76 (SOP); ECR 0.39 → 0.59 / 0.61.
- **TrainTicket:** smaller and mixed. JRA 0.58 → 0.56 (ICL) / 0.62 (SOP); ECR essentially unchanged (0.18, 0.18, 0.17).

The explanation is retrieval granularity: the same symptom can come from several root causes, so retrieved examples may not match the current mechanism; SOPs are broader but less specific.

**Relevance to us: a cheap middle step for v4.**
- Between "agent from scratch" and "deterministic recipe" (the Crystallization paper's Types 3 and 1), there is a third option: **give the agent its own past successful traces as examples**. It is cheap to build from our saved traces and fits the Optional item of our R2 ("few-shot examples from past fixes").
- **The retrieval-granularity warning applies to us directly.** Our knowledge signature is essentially a symptom-level key: class, reason, normalized error lines. Two different causes can share it (F4 and F8 both end in a missing env value). Retrieval keyed only by the signature could inject the wrong example. Keying by signature plus the file the fix touched, or by the evidence-graph milestones reached, would be safer.
- **SOPs and skills.** The SOP variant is close to the "skills" idea in our MCP research notes (a `SKILL.md`-style procedure per failure category).

### 4.5 Failure Modes

**What it says.** A qualitative look at failed trajectories (JRA = 0), grouped into four forms:
1. **Evidence-grounding failures:** useful evidence retrieved but misread, or underweighted (an event already says "missing secret" but the agent keeps looking elsewhere). Anchoring on a loud downstream symptom (frontend 503, a CPU alert) as the cause. Treating missing evidence (empty endpoints, no logs) as "nothing found" instead of a signal.
2. **Search-control failures:** committing to a hypothesis after one abnormal pod; not changing direction when a path is ruled out; or continuing to query after decisive evidence (`replicas=0`).
3. **Target-resolution failures:** blaming the service that reports an error instead of the shared dependency (a database, a gateway); confusing neighbouring mechanisms (probe port vs protocol).
4. **Tool-grounding failures:** wrong parameters or scope; or a valid but mis-specified query that creates misleading evidence, for example probing the wrong port and treating the resulting connection failure as a real symptom.

**Relevance to us.**
- **F10 matches modes 1 and 3.** The crashing pod is the frontend (the downstream victim). The cause is the renamed api Service. Our agent fixed the downstream config to match the renamed Service instead of restoring the Service. The fix works, but this is the "blame the component that reports the error" pattern.
- **Mode 2's early stop explains the missing git history step.** Seeing that a Service named `api-svc` exists was enough for the agent to act, so it never checked `git_log` on `api.yaml`.
- **The same four modes can label our own failed runs.** For example, our guardrail rejections and reviewer rejections could be tagged with one of the four modes in the event log.
- **Mode 4 is partly a safety issue for us.** Our tools are read-only, so a mis-specified query cannot break anything, but it can still produce misleading evidence. Injected text in logs (our F8) is the adversarial version of the same problem.

### 5. Discussion (5.1 Implications, 5.2 Future Work, 5.3 Threats to Validity)

**What it says.**
- **5.1:** replay makes controlled experiments possible: compare planners, memory, topology retrieval or tool-selection policies with the evidence held fixed. Evidence graphs show *where* an investigation fails (did not traverse a dependency, reached the component without confirming the mechanism, stopped early). Trajectories aligned to milestones make experience comparable across models and could support experience retrieval, skill distillation and process-aware training.
- **5.2 future work:**
  - state-adaptive evidence acquisition (choose the next tool by grounded milestones and expected information gain, with topology-aware planning and uncertainty-aware stopping);
  - transferable diagnostic skills invariant to component names and command syntax;
  - RCA with concurrent faults, delayed effects and missing or contradictory evidence.
- **5.3 threats:**
  - external validity: Kubernetes only, two workloads; tied to the Kubernetes version, with re-capture planned on upgrades;
  - internal validity: scaffold, prompting, nondeterminism and prior knowledge, mitigated by an identical framework and settings;
  - construct validity: only evidence inside the captured interval; faults needing long history or post-verification observations are out of scope.

**Relevance to us.**
- **Transferable skills** invariant to identifiers is the same goal as our cross-namespace learning (F9: the same fault in `demo-b` reuses the fix learned in `demo`). Their milestone alignment is a better basis for it than our current text-based signature.
- **Uncertainty-aware stopping** fits our `escalate` tool: the agent should stop and escalate when the evidence does not support one diagnosis.
- **Construct validity.** Their limit, "no observations after verification", is exactly where our loop continues: we apply a fix and observe what happens next. Repair outcomes cannot come from a frozen snapshot. A full evaluation of our system needs both: replay for diagnosis, live runs for repair and verification.

### 6. Related Work and 7. Conclusion

**What it says.** RCA has moved from discriminative localization over metrics, logs and traces (causal or graph models), to LLM generation over a provided context, to agentic workflows that choose tools and revise hypotheses. AIOps benchmarks are static (deterministic but non-interactive), OpenRCA (code over supplied files), or live (AIOpsLab, ITBench, AIOpsArena, which are interactive but variable and outcome-scored). Cloud-OpsBench fills the combination: replay with matched observations plus evidence-graph scoring.

**Relevance to us.** References worth following up:
- **MicroRemed** (their reference 55): *Benchmarking LLMs in Microservices Remediation*, arXiv 2511.01166. A remediation benchmark, which is the half Cloud-OpsBench leaves out and the closest to our thesis. It should be our next paper to read.
- **ChangeRCA** (their reference 51): root causes from software changes, which fits our git-history tools.
- **Flow-of-Action** (36) and **RCAFlow** (12): SOP-guided agents.
- **AIOpsLab** (8) and **ITBench** (18): the live benchmarks they compare against.
- **Mutiny! How Does Kubernetes Fail** (4), DSN 2024: a source for our failure taxonomy.

---

## 4. What the paper does not give us

- **No remediation.** It does not cover fixing, pull requests, GitOps sync or post-fix verification. Its snapshot paradigm cannot evaluate them by design.
- **No change history as an evidence channel.** None of the 12 tools reads git commits or deployment history, although many Kubernetes faults are caused by a recent change.
- **No CI/CD pipeline failures and no confidential-computing failures** in the taxonomy.
- **No safety or adversarial evaluation.** No injected instructions in logs, no tool poisoning.
- **One agent scaffold** (ReAct, 20 steps). Different scaffolds could change the ranking.
- **Our models are not in it.** The latest Claude model evaluated is Claude-Sonnet-4, and `gpt-oss-120b` is not evaluated.

## 5. What we take into the thesis

| Idea from the paper | How we would use it | Effort |
| --- | --- | --- |
| Evidence graph per fault | Write 3–4 milestones per fault for F1–F16 (catalog field `evidence_graph`) | Low |
| MC / EOC / ECR / EE / RAR | Compute from saved agent traces; report next to "healed" | Low–Medium |
| Outcome–process gap | Report both for v2, v3, v4; F10 as the worked example | Low |
| Process gate | Guardrail or reviewer note when mandatory milestones are missing from the trace | Medium |
| Promote only grounded runs | v4 recipe promotion requires closed evidence graphs, not only health | Low (once ECR exists) |
| Snapshot and replay | MCP record/replay mode, so agents and providers diagnose identical incidents | Medium |
| Automated Verifier for injections | "Inject → confirm the symptom (restart if needed) → hand over" step in the fault injector | Medium |
| Experience reuse as ICL | Give the agent its past successful traces; key retrieval by more than the symptom | Medium |
| Failure-mode labels | Tag failed runs with the four modes | Low |
| Difficulty strata | Report our results split into Easy (most faults) and Medium (F10–F12) | Low |
| Missing tools | Add read-only `k8s_nodes`, a Service-dependency view, a connectivity probe | Medium |
| External check of our agent | Run the v3 agent against the released Cloud-OpsBench snapshots through an MCP adapter for their 12 tools | High; gives an external diagnosis number |

Thesis research questions it touches:
- **R1 (evidence):** which evidence channels are needed. Their four channels plus our change-history channel give a clear frame.
- **R2 (diagnosis):** outcome *and* process metrics; open-weight models for in-enclave analysis.
- **R4 (repair and learning):** promote only evidence-closed runs; experience reuse.
- **R5 (safety):** the process gate as a guardrail; ungrounded diagnosis is a risk when the output is a pull request.

## 6. Questions to raise with the supervisors

1. Should the thesis evaluation include a replay-based diagnosis comparison (same incident for every agent and provider), next to live repair runs?
2. Is evidence-graph annotation for our faults worth the effort, given it gives the thesis a process metric beyond "healed"?
3. Should we try running our agent on the public Cloud-OpsBench snapshots, for an external diagnosis number, even though they cover diagnosis only?

## References

- Y. Wang, G. Yu, H. Huang, Y. Huang, Z. Wang, P. Chen, M. R. Lyu, *Cloud-OpsBench: A Reproducible Benchmark for Agentic Root Cause Analysis in Cloud Systems*, arXiv:2603.00468v2, 2026. [PDF](https://arxiv.org/pdf/2603.00468), [code and data](https://github.com/LLM4Ops/Cloud-OpsBench)
- Follow-ups named by the paper: MicroRemed (arXiv 2511.01166), ChangeRCA (FSE 2024), Flow-of-Action (WWW 2025), RCAFlow (AAAI 2026), AIOpsLab (MLSys 2025), ITBench (ICML 2025), Mutiny! (DSN 2024).
- Our documents: [plan-v3/failure-taxonomy.md](../plan-v3/failure-taxonomy.md), [plan-v3/prototype-v3.md](../plan-v3/prototype-v3.md), [plan-v3/prototype-v3-results.md](../plan-v3/prototype-v3-results.md), [progressive-crystallization-overview.md](progressive-crystallization-overview.md).
