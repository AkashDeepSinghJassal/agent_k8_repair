# SREGym: paper overview and relevance to our thesis

**Paper:** Jackson Clark, Yiming Su, Saad Mohammad Rafid Pial, Yifang Tian, Lily Gniedziejko, Hans-Arno Jacobsen, Yinfang Chen, Tianyin Xu, *SREGym: A Live Benchmark for AI SRE Agents with High-Fidelity Failure Scenarios*, arXiv [2605.07161](https://arxiv.org/pdf/2605.07161) **v3, 30 July 2026**, cs.AI. University of Illinois Urbana-Champaign and University of Toronto. 26 pages (9 main pages and appendices A–I). Open source: https://github.com/SREGym/SREGym.

**Our thesis:** Kubernetes-Native AI Repair Loop for Confidential GitOps Pipelines Using SCONE. Prototypes:
- v2: one-shot analyzer;
- v3: MCP agent;
- v4: hybrid with progressive crystallization (in progress on branch `prototype-v4`).

All of them repair through reviewed pull requests that Argo CD applies.

**How this overview is built.** I read the full PDF. Numbers come from its text and tables. Per-problem results in its Figures 7–8 are heatmaps whose cell values I cannot read from the text, so I do not quote per-problem scores. "Relevance" paragraphs are my analysis.

---

## 1. The paper in one paragraph

Existing SRE benchmarks are too simple: mostly application-layer faults, one fault in an otherwise clean system, and hard-coded scenarios that are hard to extend. SREGym is a **live** benchmark:
- **What it runs:** real cloud-native applications on Kubernetes, with fault injectors across the whole stack (application, Kubernetes, operators, OS kernel through eBPF, disk hardware).
- **What it adds:** ambient **noise** (unrelated transient failures), and three harder failure modes: **metastable**, **concurrent** and **correlated** failures.
- **Size:** 90 curated problems from 47 fault primitives, which could form 3,623 valid fault-target pairs.
- **Interface:** agents reach the system through **MCP servers** (metrics, logs, traces, kubectl, submission).
- **Scoring:** a checklist-based LLM judge scores the diagnosis; a state-based oracle scores whether the failure was really mitigated.

Frontier agents (Stratus, Claude Code, Codex with Sonnet-4.6, GPT-5.4, Kimi K2.5) reach 38–73% diagnosis and 40–79% mitigation success. Noise hurts every agent, and on SREGym's genuinely new failure types end-to-end success falls to 15–28%.

## 2. Why it matters for us, in one paragraph

SREGym is the closest benchmark to our setting: live Kubernetes, MCP tools, diagnosis **and** mitigation. It gives us four things.
- **Faults:** a large, validated list of production-style faults to test against. That list is now the basis of our master fault list (`research/fault-catalog-master.md`).
- **Principles:** design principles we already follow and can cite. "Simulate faults, not symptoms": our faults are defects in git, not chaos tools.
- **Failure patterns:** above all the "greedy" first-anomaly fixation and the effect of noise. We do not test either yet.
- **Oracle design:** the checklist-based diagnosis oracle (localization, characterization, scope), which we can reuse to score our proposals.

Its main contrast with us is **how fixes happen**. SREGym's agents mutate the live cluster with `kubectl patch`, `kubectl rollout` and so on. Ours change git through a reviewed pull request. That difference is our thesis's safety and audit argument.

---

## 3. Section by section

### Abstract and 1. Introduction

**What it says.** AI coding agents create more operational risk: AI-generated code is reported to have 1.7× more defects, and 43% of AI code changes escape testing into production. So agents must also mitigate production incidents, which is site reliability engineering (SRE). SRE differs from coding: reasoning over configuration, metrics, logs and traces, using domain tools, and multi-step mitigations whose effect is only visible at runtime.

Existing benchmarks fall short in several ways:
- Static Q&A and RCA datasets cannot test resolution.
- AIOpsLab and ITBench are live, but mostly application-layer, single-fault, noise-free and hard to extend.

SREGym's three new features: faults across the stack (hardware, kernel, misoperations), ambient noise, and metastable, concurrent and correlated failure modes. Headline results: diagnosis 38.1–72.6%, mitigation 40.4–78.5%, up to 40% end-to-end difference between failure kinds; compound failures get partial conclusions; noise affects agents non-trivially.

The paper uses the classic fault → error → failure terminology:
- a *fault* is the root cause;
- an *error* is the abnormal behaviour it causes;
- a *failure* is what users see.

**Figure 1** shows the problem `missing_env_variable_astronomy_shop`: the frontend loses its `CART_ADDR` environment variable.
- Codex fixates on a product-catalog pod's 20 MiB memory limit (a red herring) and fails.
- Stratus traces failing cart calls, inspects the frontend, finds `CART_ADDR` missing and restores it.

**Relevance to us.**
- **Figure 1 is almost our F4/F8.** Our F4 removes `DB_URL` from the api's ConfigMap. The difference: in SREGym the frontend keeps running and only requests fail, while our demo app crashes on start without the variable. That crash is why our operator can detect it at all (our detectors only see crashing, unschedulable or failed pods; see §2.4 and §4 below).
- **The fault → error → failure terminology is worth adopting in the thesis.** Our detectors observe the *failure* (a crashing pod). The agent must find the *fault* (a wrong line in git). Our F10 shows the gap: the failure is in the frontend, the fault is in the api Service manifest.
- **The "AI writes buggy code" motivation is an argument for our safety design.** If an AI repair loop writes changes, they must pass the same review and guardrails as human changes.

### 2. SREGym design (principles)

**What it says.** Four principles:
1. **Noisy, eventful environments.** Clean systems are unrealistic and too easy.
2. **Simulate faults, not symptoms.** Chaos tools create symptoms (killed pods, dropped packets) that can only be "mitigated" by stopping the chaos tool. SREGym injects real defects.
3. **Composability.** Faults and noises are composed into scenarios; Ansible-script designs could not express distributed timing.
4. **Usability and extensibility.** Push-button, with an API for new problems.

**Figure 2 (architecture):**
- **applications:** DeathStarBench, TrainTicket, Astronomy Shop, an in-house satellite simulator, flight booking;
- **backends:** Kafka, TiDB, MongoDB, MySQL;
- **injectors:** hardware faults, code bugs, bad operations, misconfigurations;
- **noise models;**
- **observability:** traces, logs, metrics, state;
- **problem specs:** system, fault, oracle;
- **agent tools:** exposed through MCP.

**Relevance to us.**
- **Principle 2 is our design.** Our faults are git commits that leave a defect (wrong limit, missing key, renamed Service). The only correct fix is to change git, not to restart something. The paper criticises MicroRemed (all faults from Chaos Mesh), Cloud-OpsBench (partly ChaosBlade) and about 16% of ITBench (Chaos Mesh schedules) for this. Citing it lets us argue our fault design is the defensible one.
- **Principle 1, noise, is a gap.** Our demo runs one fault in a clean cluster. That is the "oversimplistic" setting the paper criticises, and the reason our v3 agent looks good on F1–F16.

### 2.1 Problem definition

**What it says.** A problem is a four-tuple `P = (E, I, F, O)`:
- **E:** the system environment (applications, backends, Kubernetes controllers);
- **I:** the agent interface (tools and APIs);
- **F:** faults and noises (some of F may be designated noise: transient, low-impact disturbances);
- **O:** the oracles, a diagnosis oracle and a mitigation oracle.

The agent observes E through I and submits twice: a natural-language diagnosis (scored by the diagnosis oracle), then a signal that mitigation is done (scored on live system state). Programmatic verification is used wherever possible.

**Relevance to us.**
- **Our faults map onto this tuple:**
  - E is the demo app on kind;
  - I is our 14 MCP tools;
  - F is one fault per run today;
  - O is our verify step (Argo CD sync plus health window), which acts as a mitigation oracle.
- **We have no diagnosis oracle.** We only check whether pods became healthy. This four-tuple suggests adding one (see §2.5).
- **Our two "submissions":** the agent's diagnosis is the `root_cause` it gives in `propose_patch`, and mitigation is the merged PR plus the verify window.

### 2.2 Fault and noise simulation (Table 1), system environments

**What it says.**

| Mechanism | Simulated fault |
| --- | --- |
| Kill a process or pod | Fail-stop behaviour |
| Stress hardware (stress-ng) | Fail-slow behaviour |
| Fail syscalls via eBPF | OS and hardware faults |
| Corrupt a disk sector (dm-dust) | Disk sector errors |
| Fault in deploy.yaml | Service mis-deployment |
| Fault in application config | Application misconfiguration |
| Fault in Kubernetes config | Kubernetes misconfiguration |
| Buggy application code | Code bugs |
| Buggy application operator | Misoperations |
| Increase client load | Service overloads |
| **Noise:** pause or restart unrelated pods | Temporary pod failures |
| **Noise:** inject latency or drop packets | Network delay and jitter |
| **Noise:** stress node resources | Noisy neighbours |

Environments are Kubernetes with applications in containers, managed by operators and deployed with Helm. Any application with manifests or a Helm chart can be added.

**Relevance to us.**
- **Our faults sit in three of the ten mechanisms:** "fault in deploy.yaml", "fault in app config" and "fault in Kubernetes config". These are exactly the ones a GitOps loop can fix, because the defect lives in git.
- **We can't fix the other mechanisms through git, but we could add noise.** OS, hardware and load faults are out of scope for repair by pull request; the right behaviour is escalation, with a useful diagnosis. The noise row is cheap to add to our demo: restart an unrelated pod every few minutes, or give a healthy deployment a suspiciously low memory limit (the Figure 1 red herring). It would test whether our agent fixates on the first anomaly.

### 2.3 Agent interface (MCP)

**What it says.** No assumptions on the agent's architecture. SREGym exposes MCP servers:
- **metrics:** Prometheus;
- **logs:** Loki;
- **traces:** Jaeger;
- **cluster control:** any `kubectl` command, read and write;
- **submission:** submit diagnosis and mitigation.

Power users can use the raw observability and Kubernetes API endpoints.

**Relevance to us.**
- **The same design choice as our v3:** the agent sees the world only through MCP servers, and a dedicated server receives the result. Their "submission" server is our `repair` server (`propose_patch`, `escalate`). A recent, well-maintained benchmark choosing the same interface supports our design.
- **The key difference is write access.** Their cluster-control server allows any `kubectl` command, including writes. Ours is read-only by construction; the only "write" is a proposal that becomes a reviewed pull request. On SREGym our agent could not "mitigate" by itself. It would need a merge (and an Argo CD sync) inside the time budget. Running our agent on SREGym would therefore need a mode where the operator auto-applies after guardrails, which is worth noting as a limitation if we ever compare.
- **We lack the observability servers they offer:** metrics, logs search and traces. With them, our agent could diagnose failures that do not crash a pod (request failures, latency), which our detectors cannot even see today.

### 2.4 Composing problems; failure modes (Fig. 4)

**What it says.**
- **Scale:** 90 problems (continuously growing); 47 fault primitives over 139 deployable services in 5 applications; 3,623 valid fault-target pairs (Appendix C), so the 90 curated problems exercise about 2.5% of the space.
- **Noise:** transient, self-recovering disturbances injected on a schedule; agents see both and must tell them apart.
- **Three failure modes:**
  - **Metastable:** a transient trigger pushes the system into a self-sustaining degraded state that persists after the trigger is removed. Example: gRPC timeout of 50 ms with 30 retries, 3,000 requests per second, then a short CPU stress, giving a retry storm. Hard to diagnose because nothing crashes.
  - **Concurrent:** independent faults at the same time. Example: a scheduler misconfiguration makes an observability pod unschedulable, while a port misconfiguration breaks the user-ID service. The agent should prioritise the one users feel.
  - **Correlated:** components fail together through a shared cause or dependency.

**Relevance to us.**
- **Concurrent failures fit our design and are worth adding.** Two faults at once (for example F14, a scheduling failure on the worker, plus F4, the api crash) would create two incidents. Our operator handles incidents independently, and the "sibling" logic merges signals for the same workload. Questions a compound fault would test:
  - do two agents running in parallel interfere?
  - does the dashboard make the priority clear?
  - does each PR fix only its own fault?
- **Correlated failures** relate to our fingerprinting: a shared ConfigMap breaking two Deployments should ideally be one root cause, not two incidents with two PRs.
- **Metastable failures are out of reach for us:** no load generator, no metrics, and our detectors do not see degradation without a crash. Say so in the thesis scope.
- **The 3,623-pair idea:** our faults are also "primitive × target". F1 on `api` and F9 (the same fault on `api-b`) are exactly that. A parameterised injector could grow our catalog far more cheaply than hand-written entries.

### 2.5 Evaluation oracles (Table 2, Appendix A)

**What it says.** Label-matching oracles are brittle: agents often give correct answers that do not match predefined labels. Topology-aware matching (ITBench) needs too much annotation.

**Diagnosis oracle:** a checklist-based LLM judge with 9 yes/no questions in 3 dimensions, equally weighted. Pass threshold 7/9, which forbids passing while missing a whole dimension.

| Dimension | Questions (abbreviated) |
| --- | --- |
| Fault localization | names the right originating component; distinguishes it from downstream victims; does not blame a healthy component |
| Fault characterization | same injected mechanism (wrong port, missing env var, wrong image, wrong selector, memory limit); includes the concrete mutated detail; no wrong fault type |
| Failure scope | does not blame uninvolved components; includes all contributing components; describes the impact consistently |

Agreement with a human expert on 100 stratified samples: Cohen's κ = 0.90 for the default judge (Sonnet-4.6); 0.70 and 0.76 for GPT-5.4 and Kimi K2.5; 0.94 between the two alternate LLM judges.

**Mitigation oracle:** problem-specific, checks that the fault is resolved and the system healthy, from both client side (request success rate) and system side (processes, cluster).

**Relevance to us.**
- **Directly reusable.** Each of our catalog entries already has `expected_class` and `correct_fix`, and v4 adds `evidence_milestones`. A judge that scores the agent's `root_cause` against the catalog with these nine questions gives a diagnosis score per run. A cheap model (Haiku) would do for the judge, though the κ values in the paper are for Sonnet-4.6, so agreement with a smaller judge would need its own check.
- **F10 under this checklist:** our F10 fix would likely fail "distinguishes origin from downstream victims". The agent changed the frontend's nginx config, the victim, rather than restoring the api Service, the origin. That matches what the Cloud-OpsBench overview found with process metrics: two independent methods flag the same run.
- **Our mitigation oracle is health-only.** SREGym's is problem-specific and includes the fault itself being resolved. For us, "fault resolved" can be checked in git: the catalog's breaking edit should no longer be present. Our dashboard already checks this to clear the "injected" badge. Combining the two (pods healthy *and* defect gone from git) would make our verification closer to theirs.

### 3. Results (Tables 3–5, Fig. 5)

**What it says.** Agents:
- Stratus (an SRE multi-agent system) with Sonnet-4.6 and with Kimi K2.5;
- Claude Code with Sonnet-4.6;
- Codex with GPT-5.4.

Three runs per problem; noise is two random patterns every five minutes, each lasting two minutes; 1,800 s timeout.

**Table 3 (overall):**

| Agent | Noise | Diag. | Mitig. | E2E | TTD (s) | TTM (s) | Tokens |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Stratus, Sonnet-4.6 | no | 61.5% | 78.5% | 54.8% | 114 | 771 | 812K |
| Stratus, Sonnet-4.6 | yes | 51.5% | 61.1% | 39.6% | 171 | 885 | 464K |
| Stratus, Kimi K2.5 | no | 40.4% | 40.4% | 27.4% | 675 | 1,349 | 413K |
| Stratus, Kimi K2.5 | yes | 38.1% | 41.9% | 26.7% | 656 | 1,283 | 443K |
| Claude Code, Sonnet-4.6 | no | 72.6% | 75.6% | 60.7% | 293 | 702 | 1.47M |
| Claude Code, Sonnet-4.6 | yes | 62.6% | 76.3% | 53.7% | 314 | 737 | 1.71M |
| Codex, GPT-5.4 | no | 70.0% | 63.7% | 53.3% | 176 | 376 | 1.98M |
| Codex, GPT-5.4 | yes | 59.3% | 61.9% | 45.9% | 218 | 398 | 1.88M |

Findings:
- **Agent characteristics:** Claude Code has the best end-to-end rate. Stratus with Sonnet has the best mitigation, which the paper credits to its undo-and-retry mechanism. Kimi trails because of model capability.
- **Tokens:** Claude Code uses 1.81× and Codex at least 2.44× the tokens of Stratus with the same model. Coding agents ingest raw observability data; Stratus preprocesses it.
- **Noise:** noise lowers diagnosis for every agent; mitigation is more robust. All agents are **greedy**: they treat the first plausible anomaly as the target, and sometimes found evidence of the real cause but ignored it.
- **New failures (Table 4):** problems ported from AIOpsLab/ITBench (34) and similar new ones (43) are much easier than SREGym's genuinely new failure types (13). E2E without noise: Stratus-Sonnet 63.7% → 17.9%, Claude Code 60.8% → 28.2%, Codex 57.8% → 15.4%.
  - **Hardware:** for `latent_sector_error` no run scored above 0.22 and characterization was always 0. Agents blamed application error handling and never ran disk diagnostics.
  - **Metastable:** agents found the application trigger but never the resource constraint and its interaction.
- **Diagnosis vs mitigation (Table 5):**
  - with a correct diagnosis, mitigation succeeds in 68–89% of cases;
  - with a wrong one, 22–62%. Agents either pattern-match a symptom to a known fix, or recover by watching the failure persist and forming new hypotheses (Stratus-Sonnet: 3.82 mitigation attempts with a wrong diagnosis vs 1.88 with a correct one).
- **Tools (Fig. 5):**
  - 60–72% of calls are read-only `kubectl`, and `get` plus `logs` make up about 87% of reads (mostly Pods, Deployments, ConfigMaps);
  - agents make about 19–28 reads before their first write;
  - Stratus prefers `kubectl patch`, Claude Code and Codex prefer `kubectl rollout` restarts;
  - Codex often spins up test containers and port-forwards.

**Relevance to us.**
- **Our agent's tool use matches theirs in kind.** Our live v3 runs used `k8s_describe_pod`, `k8s_logs`, `k8s_get` and `git_read` most, which are their `get`/`logs`/`describe` pattern. Our runs needed 9–11 calls in total rather than 19–28 reads before a first write. But our faults are single, noise-free and crash-visible, the "easy" end of their spectrum, so this is not a like-for-like comparison.
- **Token cost:** their agents use 0.4–2.0 million tokens per run. Our one measured v3 run used about 61k input tokens. Again the scenario difficulty differs. The structural point that matters for v4 is theirs: **preprocessing evidence (Stratus) costs a third of the tokens of letting a general agent read raw data, for similar success**. That is an argument for a T2 hybrid tier: deterministic evidence collection, then one narrow model call.
- **Undo-and-retry is the best mitigation strategy here,** the same point as the Hyperscale paper's automatic rollback. In our design that is a revert PR when verification fails, which we do not yet do.
- **"Pattern-matching a symptom to a known fix"** is how mitigation sometimes succeeds with a wrong diagnosis. It is also exactly what a crystallized T1 playbook does: on purpose, and only after evidence. The difference that must hold in v4: T1 is allowed only for a signature that has repeatedly been fixed correctly, and it demotes on the first failure.
- **Greedy diagnosis plus noise suggests a v4 experiment:** run F4 with and without a red-herring noise (an unrelated deployment with a tiny memory limit), on T3 and on T1. T1 replays a fixed evidence path and should be immune to the distraction. T3 may not be. That would be an unusual, positive result for crystallization.
- **Restarts as mitigation.** Codex and Claude Code reach for `kubectl rollout`. In our GitOps setting a restart never fixes a git defect, and Argo CD would revert a direct patch anyway. This supports our choice of git as the only write path.

### 4. Related Work; 5. Discussion and Conclusion

**What it says.** Agentic SRE spans RCA to mitigation, with many commercial products (Azure SRE Agent, AWS DevOps Agent, Resolve.ai and others). Static datasets cannot support mitigation; existing live benchmarks lack event orchestration, low-level fault injection and noise. Next steps: richer noise, more fault types and failure modes, edge environments, and turning SREGym into a reinforcement-learning training ground.

**Relevance to us.**
- **RL training on many fault-target pairs** connects to the "fine-tuned small model" stretch idea in our blueprint, and to the open-weight-model point in the Cloud-OpsBench overview. A small model trained on such episodes could run inside a SCONE enclave.
- **Edge environments** are listed as future work; confidential computing is not mentioned at all.

### Appendix B: Engineering practices

**What it says.**
- **Live, not snapshots.** Snapshot benchmarks (it names Cloud-OpsBench) "reduce the task to static log triage" and remove the skills that matter: forming hypotheses, probing, watching the system respond.
- **No restriction on agent architecture:** only `submit()` is required, so Stratus, Claude Code and Codex all run unchanged.
- **A programmable runtime** to time faults, noises and oracle probing in one process (needed for metastable problems).
- **Avoid misusing chaos tools** (symptoms, not defects).
- **Reward-hacking protection:**
  - agents could find and disable fault-injector pods (AIOpsLab and ITBench run them visibly);
  - alert-clearing as success is gameable: 8 of 18 ITBench mitigation problems (44%) can be "solved" by a pod-restart loop, because the injector loses track of the restarted pod.

  SREGym hides injection behind a proxy and uses state-based oracles.

**Relevance to us.**
- **Snapshot vs live is a real disagreement between the two benchmarks.** Cloud-OpsBench argues for snapshots (controlled comparison); SREGym argues for live (realistic interaction). For our thesis the reasonable position is both: replay for comparing diagnosis between tiers and providers on identical evidence, and live runs for the full repair loop.
- **The pod-restart reward hack has a direct analogue in our system.** Our F4, F8, F10, F11 and F12 only show after a pod restart. A naive verifier could call the incident "healed" because the old pod is still running, even though git still holds the defect. We hit this when a stuck rollout kept the old pod Ready. Verifying that the defect is gone from git, not only that pods are healthy, closes this hole.
- **Injector visibility:** our injector commits to git, so an agent reading `git_log` sees commits titled `fault(F4): …`. That is a giveaway our agent can exploit. For a fair evaluation, fault commits should look like ordinary changes (for example "update api config") rather than naming the fault.

### Appendix C: Combinatorial coverage (Table 7)

**What it says.**

| Fault class | Primitives | Targets | Pairs |
| --- | --- | --- | --- |
| Universal Kubernetes-level | 25 | 139 pods | 3,475 |
| Storage-dependent | 5 | 6 PVC-mounted pods | 30 |
| DaemonSet-level | 1 | 3 DaemonSets | 3 |
| Operator-level | 6 | 5 applications | 30 |
| MongoDB-specific | 4 | 18 pods | 72 |
| Valkey-specific | 2 | 1 pod | 2 |
| App-layer misconfiguration | 1 | 2 services | 2 |
| Node/kernel | 3 | 3 worker nodes | 9 |
| **Total** | | | **3,623** |

**Relevance to us.** 25 of 47 primitives are "universal Kubernetes-level". Those are the ones a GitOps loop can mostly fix in git, and the class our catalog should grow in. A parameterised version of our injector (primitive × target deployment) is a cheap way to scale our evaluation, and a natural way to test v4's cross-namespace learning (does a playbook learned on `api` transfer to `api-b`?).

### Appendix D: Case studies

**What it says.**
- **D.1 Metastable:**
  - *Setup:* an application trigger (aggressive retries or a low `GOGC`) plus an infrastructure constraint (a tight namespace quota). The constraint produces no errors and can only be found by reading deployment configuration.
  - *Agent behaviour:* agents found the trigger but almost never the constraint. In one `gc_capacity_degradation` run, the agent never checked `GOGC` and invented a network-latency rule.
- **D.2 Hardware:**
  - *Setup:* intermittent `pread()` errors crash MongoDB with "input/output error".
  - *Agent behaviour:* agents blamed memcached, the workload or the application's error handling, and never suggested disk diagnostics (`dmesg`, `smartctl`).
- **D.3 Greedy diagnosis:**
  - in `missing_env_variable_astronomy_shop` no agent ever inspected the frontend;
  - in `valkey_auth_disruption` the agent saw evidence pointing at authentication (a successful TCP health check, but a failing application connection), named `requirepass` as a hypothesis several times, and still settled on "low memory".
  - Agents treat the first plausible abnormality as a stopping criterion, unlike a human who keeps competing hypotheses.

**Relevance to us.**
- **D.3 is the strongest argument in the paper for v4's design.** A T3 agent can be distracted. A crystallized T1 or T2 playbook replays the evidence path that led to a *verified* fix. It cannot be lured away by a new anomaly, because it does not choose what to look at.

  The flip side is that T1 cannot notice when the situation really is different. That is why the router must check the signature carefully and demote on any failure.
- **A prompt-level mitigation for T3:** ask the agent to list at least two hypotheses and the evidence that would separate them before calling `propose_patch`. It is cheap to try and measurable with the diagnosis checklist.
- **D.2 confirms our scope choice:** hardware and kernel faults are not fixable through git. The useful behaviour is to escalate with a diagnosis that at least names the layer.

### Appendices E–I

**What it says.**
- **E (tool usage details):**
  - reads outnumber writes 5.5:1 to 14.5:1;
  - top reads are `get` (57–69%), `logs` (17–25%), `describe` or `exec`;
  - top writes are `patch` (Stratus) and `rollout` (Claude Code, Codex);
  - mitigation attempts and reads rise sharply after a wrong diagnosis (Table 12).
- **F (tokens vs success):** Stratus about 0.53M tokens per run on average; Claude Code 1.59M (3.0×); Codex 1.93M (3.6×). "The marginal token does not buy additional success."
- **G (broader impacts):**
  - automation displacement is distant;
  - the injectors are standard tools, so they add no new attack capability;
  - read scores as a lower bound on what an agent can recover from, not as clearance for production.
- **H (limitations):**
  - the LLM judge adds variance;
  - noise is a simplification (no traffic anomalies, partial partitions or slow exhaustion);
  - scale is far below production (largest application 40 services, versus thousands in industry);
  - Kubernetes-only;
  - three agents and three models.
- **I:** per-problem end-to-end heatmaps with and without noise.

**Relevance to us.**
- **"The marginal token does not buy additional success"** is a direct, citable motivation for v4's goal of cutting tokens without losing success.
- **The scale limitation applies even more to us** (four services plus one in a second namespace); state it plainly in the thesis.
- **The broader-impacts point about benchmark scores** applies to our results too: healing our injected faults is a lower bound, not evidence of production readiness.

---

## 4. What the paper does not give us

- **No GitOps or review-gated changes.** Agents mutate the cluster directly. Argo CD self-heal, which would revert those changes in a GitOps cluster, is not discussed.
- **No learning across runs.** Each run is independent; there is no memory, no playbooks and no crystallization.
- **No confidential computing,** attestation or protection of diagnostic data.
- **No per-problem numbers in text form** (heatmaps only), and three runs per problem.
- **No CI/CD pipeline failures** (our F6).

## 5. What we take into the thesis

| Idea | Our step | Effort |
| --- | --- | --- |
| SREGym problem list | Seed the master fault list (`research/fault-catalog-master.md`) and new faults F17+ | Done in v4 issue #27 |
| Faults, not symptoms | State and cite it as our fault-design principle | Low |
| Noise | Add red-herring noise to the demo (an unrelated low memory limit; unrelated pod restarts) and compare T3 vs T1 under noise | Low–Medium |
| Concurrent faults | Inject two faults at once; check independence and priority | Low |
| Checklist diagnosis oracle | Score each proposal's root cause with the 9-question checklist against the catalog (a Haiku judge) | Low–Medium |
| Defect-gone verification | Verify both pod health and that the breaking edit is gone from git | Low |
| Reward-hack hygiene | Neutral commit messages for injected faults, so `git_log` does not reveal the fault | Low |
| Undo-and-retry | Revert PR on failed verification (also from the Hyperscale paper) | Low–Medium |
| Competing hypotheses | Prompt the T3 agent to name two hypotheses and the separating evidence | Low |
| Primitive × target | Parameterise the injector to apply a primitive to any deployment; test playbook transfer | Medium |
| Observability servers | Metrics or logs-search MCP servers, to reach non-crashing failures | Medium–High |

Thesis research questions it touches:
- **R1 (evidence):** which observability modalities are needed; tool-use patterns.
- **R2 (diagnosis):** the checklist oracle; greedy diagnosis under noise.
- **R3 (cost):** tokens versus success; preprocessing as the cheaper path.
- **R4 (repair):** undo-and-retry; verification that the defect is gone.
- **R5 (safety):** faults-not-symptoms, reward hacking, and git as the only write path.

## 6. Questions to raise with the supervisors

1. Should the evaluation include noise and concurrent faults, given the paper shows they change results substantially?
2. Is an LLM-judge diagnosis score (the paper's 9-question checklist) acceptable in the thesis, alongside our programmatic healing check?
3. Should we attempt to run our agent on SREGym itself? It would need an "auto-apply after guardrails" mode, because SREGym expects the agent to mitigate within a time limit, without a human merge.

## References

- J. Clark, Y. Su, S. M. R. Pial, Y. Tian, L. Gniedziejko, H.-A. Jacobsen, Y. Chen, T. Xu, *SREGym: A Live Benchmark for AI SRE Agents with High-Fidelity Failure Scenarios*, arXiv:2605.07161v3, 2026. [PDF](https://arxiv.org/pdf/2605.07161), [code](https://github.com/SREGym/SREGym)
- Named by the paper and relevant to us: Stratus (NeurIPS 2025, undo-and-retry SRE agent), AIOpsLab (MLSys 2025), ITBench (ICML 2025), MicroRemed (arXiv 2511.01166), Cloud-OpsBench (arXiv 2603.00468), "Configuration Defects in Kubernetes" (arXiv 2512.05062), "Metastable Failures in the Wild" (OSDI 2022), "Gray Failure" (HotOS 2017), "Why Does the Cloud Stop Computing?" (SoCC 2016).
- Related overviews: [cloud-opsbench-overview.md](cloud-opsbench-overview.md), [progressive-crystallization-overview.md](progressive-crystallization-overview.md), [hyperscale-incident-resolution-overview.md](hyperscale-incident-resolution-overview.md).
