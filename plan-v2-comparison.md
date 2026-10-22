# Plan v1 vs New Research Ideas: Comparison and Plan v2 Synthesis

Compares our current understanding (**plan v1**: the research board, the paper analyses and the design discussions) with two new documents:
- **Blueprint:** [confidential-repair-loop-blueprint.md](confidential-repair-loop-blueprint.md), with Plans A–E, the SCONE failure details and the autonomy ladder;
- **Prototype:** [repair-loop-prototype-implementation-plan.md](repair-loop-prototype-implementation-plan.md), step 1, a 6-week build plan.

It ends with a combined **plan v2** and the concrete changes to make to the prototype plan.

---

## 1. Short answer

The three sources agree on the core loop: detect → evidence → classify → small patch → validation → PR → human → Argo re-run → verify → learn. The AI starts outside the enclave and moves in later. They differ in **who writes the patch**, **how learning shortcuts the LLM**, **where state lives**, and **how much autonomy is allowed**.

The better solution takes:
- the **Kubernetes-native skeleton** from the new documents (operator, CRDs, phase state machine, guardrails, autonomy ladder);
- the **decision layer** from plan v1 (knowledge fingerprints, a fast path with no LLM, deterministic templates for changes that affect trust);
- **one correction to plan v1:** MRENCLAVE values come from the trusted build pipeline, not from the bot.

---

## 2. Where they agree

| Topic | Plan v1 | Blueprint / Prototype |
|---|---|---|
| Core loop | 7 steps with human review and an Argo re-run | Same 6 steps plus a verify window |
| Enclave staging | Must = outside, Optional = inside | Build step 1 outside, step 3 moves the analyzer into SCONE |
| Rules before the LLM | Rule pre-classifier for known signatures | Stage-1 rule table, also used as the RQ3 baseline |
| LLM output | Structured JSON: class + evidence lines | Structured JSON: class, root cause, confidence, `fixable_in_git`, edits |
| No cluster write access for the AI | Bot only opens PRs; the pipeline uploads to CAS | Read-only RBAC; changes only through git; Kyverno/Gatekeeper |
| Validation before the PR | Schema check, dry-run, CAS session check | 6 guardrails: allowlist, exact edit, size cap, linters, server dry-run, Conftest/OPA |
| Learning | KB of verified fixes; fast path; reviewer edits | Retrieval of past fixes, promotion after 3 verified fixes, preference pairs, calibration |
| Injection risk | Logs are untrusted (R5) | LogJack/Straiker evidence; F8 injection fault |
| Evaluation | Fault injection + real failures; LogSage and rule-only baselines | Fault harness after AIOpsLab/MicroRemed; same RQ experiments |
| Escalation | No template → escalate | `fixable_in_git: false` → `Escalated`, no PR |

**Conclusion:** there is no conflict about *what* to build. The new documents add a lot of engineering detail on *how*.

---

## 3. Where they differ, and which choice is better

| # | Topic | Plan v1 | Blueprint / Prototype | Better choice for v2 |
|---|---|---|---|---|
| D1 | **Runtime shape** | 2 services (repair-controller, learner) + Postgres; agents as modules | **Kopf operator + `RepairIncident` CRD** with a phase state machine | **Operator + CRD.** It matches the thesis title ("Kubernetes-native"), and the CRD status gives a free audit trail and timestamps for RQ4. Our agent roles become phase handlers (§4). |
| D2 | **Who writes the patch** | LLM picks a class and template; **code** fills it in | LLM writes the change: a `diff` in the blueprint, `find/replace` edits in the prototype | **Tiered (§5).** Templates for known and trust-changing classes; LLM `find/replace` only for open-ended classes such as `CRASH_LOOP` and `STAGE_FAILURE`, behind guardrails. |
| D3 | **Fingerprint** | Normalised error templates + reason + error codes, **no namespace**, to reuse knowledge | `sha256(namespace│workload│container│reason)`, to **dedupe incidents** | **We need two fingerprints (§6).** The prototype's fingerprint cannot drive cross-namespace learning; ours cannot dedupe replicas. |
| D4 | **Fast path** | Exact match on a verified entry → no LLM | "Recurrence promotion": 3 verified fixes → deterministic template | Same idea in different words. Use **candidate → verified (1×) → promoted (3×)**: verified entries become **few-shot examples**; only promoted ones skip the LLM. That is safer than our "verified once". |
| D5 | **MRENCLAVE source** | Bot runs `SCONE_HASH=1` on the new image before the PR | **Only from a trusted, reproducible build, measured in CI**; `needs_remeasure` flag | **Blueprint is right; fix plan v1.** The bot would run an untrusted image, and an attacker controlling the registry or logs could get their own enclave approved. The PR declares `needs_remeasure`, and trusted CI writes the value. |
| D6 | **Heap fixes** | Listed `SCONE_HEAP` increase as a simple template | Heap changes **MRENCLAVE** → a three-step coupled change (edit → re-measure → update session) | **Blueprint.** It is the most important SCONE-specific insight, and RQ5 should measure coupled changes separately. |
| D7 | **Autonomy** | Always human review | **Ladder L0–L4** per class in `RepairPolicy`; auto-merge only for promoted templates | **Adopt the ladder:** L0 (shadow mode) for RQ3, L2 as the thesis default, L3 only in a sandbox experiment. "Autonomy earned by evidence" is a reportable result. |
| D8 | **Knowledge-base storage** | Postgres + pgvector in plain text on the host | **Sealed** incident memory inside the trust boundary | **Split it (§6):** fix recipes and fingerprints can stay readable; raw evidence must be sealed or encrypted. Our plain store would undermine the confidentiality claim. |
| D9 | **Multi-agent** | 6 named agents | Plan A = one structured call; **Plan C = tool-using agent over MCP** as an ablation | Use our roles as **deterministic phase handlers with one LLM step**. Run Plan C as the "one-shot vs agentic" ablation. Calling A "multi-agent" would overstate it. |
| D10 | **Session file changes** | Pipeline uploads with `predecessor` | Also: `SignedPolicy`/`EncryptedPolicy` CRs are **immutable** and must be re-signed; a stale `predecessor` is fixable "like a git rebase" | **Blueprint adds detail.** The PR path must handle immutable policy CRs, and the signing key stays outside the agent. |
| D11 | **App configuration location** | Not covered | After attestation, **manifest env vars are ignored**; app config lives in the session | **Blueprint.** This creates a hidden class: "config set in the wrong place". |
| D12 | **Research questions** | F + R1–R5, with Safety as R5 and R2/R3 merged | Original RQ1–RQ5 (no safety RQ) | **Keep the board's structure.** The prototype already logs `rejectedBy` and F8, which feeds Safety directly. |
| D13 | **Baselines** | Rule-only, LogSage-style | + **Plan D** CI coding agent, + "loop **without** retrieval memory" | **Union of both.** The "without memory" baseline is exactly what tests our re-learning claim. |
| D14 | **Confidence** | Self-consistency: 3 runs, abstain if they disagree | Calibration per class; temperature 0 | **Calibration as the main mechanism;** self-consistency as an optional experiment. Note that it needs temperature > 0, which conflicts with the prototype's temperature 0 for replay. |
| D15 | **Taxonomy** | ~12 classes, not fixed | 10 classes with visibility and gate; the prototype adds `CONFIG_ERROR` | **One unified list (§7).** |
| D16 | **Dataset** | Injected + real failures, equal weight | Injected only (F1–F8), standard failures first | Injected for steps 1–2; real failures as validation once there is SGX access. |

---

## 4. Our agent roles mapped onto the operator's phases

Each plan v1 role becomes the handler for one CRD phase. Only one role calls an LLM.

| Plan v1 role | CRD phase(s) | LLM? |
|---|---|---|
| Collector | `Detected → Collected` | No |
| Triage | start of `Analyzing`: knowledge lookup, rule table | No |
| Specialist (K8s / Confidential) | `Analyzing`: one structured call; the prompt section depends on the class family | **Yes** (normal path only) |
| Patch | `Analyzing → Proposed`: render template or apply `find/replace` | No (tiers 0–2) / Yes (tier 3) |
| Verifier | `Proposed`: the 6 guardrails + SCONE "never" rules | No |
| PR writer | `Proposed → AwaitingReview` | No |
| Learner | `Healed / Failed / Escalated`: update the knowledge entry, store preference pairs | No |

That gives one operator and one model call, with clear roles that can be tested and measured separately.

---

## 5. Combined decision path (the core of plan v2)

```
failure ──► dedupe fingerprint (incident) ──► evidence bundle
                                             │
                     knowledge fingerprint ◄─┘
                             │
   Tier 0  promoted template match ───────────────► render template ─┐   (no LLM)
   Tier 1  rule-table hit ─────────────────────────► render template ─┤   (no LLM)
   Tier 2  LLM classifies + picks template/params ─► render template ─┤   (1 LLM call)
   Tier 3  open-ended class, no template ──────────► LLM find/replace ┤   (1 LLM call)
   Tier 4  fixable_in_git=false / low confidence ──► Escalate (no PR) │
                                                                       ▼
                      guardrails (6 checks + SCONE never-rules) ──► PR (L2) ──► human
                                                                       │
                        needs_remeasure? ──► trusted CI build writes MRENCLAVE into PR
                                                                       ▼
                               merge ──► pipeline uploads session / Argo CD sync ──► re-run ──► verify
                                                                       ▼
                                 learner: candidate → verified → promoted (≥3) / demoted
```

**Rules:**
- **Trust-changing classes** (`MRENCLAVE_MISMATCH`, `CAS_SESSION`, TLS/x509, policy CRs) are **template-only**: never tier 3, always 2 approvals.
- `SGX_PLATFORM` and LAS/plugin alerts always go to tier 4.
- Tier 0 is the fast path. It skips the LLM, never the guardrails or the human (except in the L3 sandbox experiment).

**Why this beats each source alone:**
- Templates make trust-changing fixes deterministic and auditable. The blueprint has the LLM write diffs even for session files.
- `find/replace` keeps generality for crash and stage failures that cannot be templated, which plan v1 had no answer for.
- The tiers *are* the learning curve: as fixes move up to tier 0, LLM calls and latency fall. That is measurable for RQ3 and RQ4.

---

## 6. Two fingerprints, and a split knowledge base

| | Dedupe fingerprint (incident identity) | Knowledge fingerprint (failure signature) |
|---|---|---|
| Purpose | One incident per broken workload, not 50 | Reuse fixes across namespaces |
| Built from | `namespace│owner workload│container or step│reason` | Kubernetes reason + exit code + step type + **normalised error-line templates** + SCONE/CAS error code + SCONE version |
| Excludes | n/a | Namespace, pod, workload names, hashes, IPs, timestamps |
| Stored in | `RepairIncident.spec.fingerprint` | `RepairIncident.status.signature` and the knowledge store |

| Store | Contents | Protection |
|---|---|---|
| `RepairIncident` CRs | Phase, timestamps, class, PR link, outcome | Plain. Must contain **no raw evidence** (the prototype already stores it by reference) |
| Evidence store | Redacted bundles, prompts, replies | Step 1: ConfigMap/PVC. Step 3+: **encrypted to the analyzer's attested key** |
| Recipe store | Knowledge fingerprint → class, template ID, extractors, counters, status | Plain is acceptable *if* it holds only templates and counters. Postgres or even a CR (`RepairRecipe`) |
| Preference pairs | Proposed vs merged patch | Sealed, because they may include config content |

---

## 7. Unified taxonomy (v2)

| Class | Visible to kubectl | Tier ceiling | Gate |
|---|---|---|---|
| OOM_KILLED (pod limit) | Yes | Template | 1 approval → L3 candidate |
| IMAGE_PULL | Yes | Template | 1 approval |
| CONFIG_ERROR (ConfigMap/Secret/env missing in the manifest) | Yes | Template | 1 approval |
| CRASH_LOOP | Yes | LLM find/replace | 1 approval |
| STAGE_FAILURE | Yes | LLM find/replace | 1 approval |
| SGX_UNSCHEDULABLE | Yes | Template / escalate if `sgxDeviceAlert` | 1 approval |
| ENV_MISSING (SCONE addresses / session env) | No | Template | 1 approval |
| CONFIG_IN_WRONG_PLACE (manifest env ignored after attestation) | No | Template | 1 approval |
| ENCLAVE_STARTUP (heap/stack) | No | Template + **needs_remeasure** | 2 approvals |
| MRENCLAVE_MISMATCH | No | Template + trusted CI value | 2 approvals |
| CAS_SESSION (stale predecessor, unknown session) | No | Template (rebase-like) | 1–2 approvals |
| SGX_PLATFORM (LAS, TCB, driver) | No | Escalate | n/a |

---

## 8. Concrete changes to the prototype plan (step 1)

All of these are small and keep the six-week schedule.

1. **Add `status.signature`** (the knowledge fingerprint) next to `spec.fingerprint`. It is about 30 lines in `fingerprint.py`, built in week 2.
2. **Templates for 3 classes** (`OOM_KILLED`, `IMAGE_PULL`, `CONFIG_ERROR`) in `analyzer/templates.py`. The LLM returns `template` + `params` for these and `find/replace` only for `CRASH_LOOP` and `STAGE_FAILURE`. Record which tier produced each proposal in `status.proposal.tier`.
3. **Store outcomes keyed by signature from day one** (counters only; no retrieval yet). This costs nothing now and gives week-6 data for the learning step.
4. **Add an L0 shadow-mode flag:** classify and log without opening a PR. This is the clean RQ3 measurement.
5. **Add fault F9, "repeat across namespaces":** inject F1 into a second namespace. The test passes when it creates a new incident but finds the same signature. That validates the fingerprint design before learning is built.
6. **Add `needs_remeasure: false` to the output schema now,** so the field exists when SCONE classes arrive.
7. **Temperature:** keep 0 for replay. If self-consistency is tested later, run it as a separate experiment at a higher temperature.

---

## 9. Updated build sequence

| Step | Blueprint | Plan v2 addition |
|---|---|---|
| 1 | Standard failures, outside the enclave (prototype, 6 weeks) | §8 changes; tiers 1–3; signature + outcome counters |
| 2 | SCONE diagnostics and hidden-class faults | Template-only trust classes; `needs_remeasure` → trusted CI measurement step; CONFIG_IN_WRONG_PLACE fault |
| 3 | Analyzer into SCONE; sealed store | Split knowledge base (§6); measure the latency delta (RQ3) |
| 4 | Guardrails and policy | SCONE never-rules; 2-approval rule; `RepairPolicy` autonomy ladder |
| 5 | Learning loop | Tier 0 promotion (≥3), demotion, few-shot retrieval; **baseline "without memory"** |
| 6 | Evaluation | L0 shadow → sandbox merges; ablations: evidence sections (RQ1), prompt formats (RQ2), Plan C agentic vs one-shot, Plan D baseline |

---

## 10. Open questions this comparison raises (for the professor)

1. Is there a **trusted build pipeline** that can measure MRENCLAVE after a change? D5 depends on it.
2. Does the team use `SignedPolicy`/`EncryptedPolicy` CRs or the `scone session` CLI? This decides how the PR path updates sessions.
3. Is auto-merge (L3) acceptable even in a sandbox experiment, or must every change keep human approval?
4. Where may the knowledge store live during the outside-the-enclave phase, and is a readable recipe store (templates and counters only) acceptable?
