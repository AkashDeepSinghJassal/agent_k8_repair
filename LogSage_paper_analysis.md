# LogSage — Paper Analysis for the Kubernetes-Native AI Repair Loop Thesis

**Paper:** W. Xu, J. Luo, T. Huang, K. Sui, J. Geng, Q. Ma, I. Akasaka, X. Shi, J. Tang, P. Cai.
*LogSage: An LLM-Based Framework for CI/CD Failure Detection and Remediation with Industrial Validation.*
ASE 2025 Industry Showcase. arXiv:2506.03691v2 (6 Oct 2025). East China Normal University + ByteDance.
**Link:** https://arxiv.org/html/2506.03691v2
**Dataset:** https://github.com/ByteLuo1029/dataset (the paper mentions no code release).

---

## 1. Summary in one paragraph

LogSage is a two-stage LLM pipeline for failed CI/CD runs.

- **Stage 1 (root-cause analysis):** it reduces a long, noisy build log to a small set of important log blocks. To do this, it compares the failed log against templates learned from recent *successful* runs of the same pipeline (a "log diff"), plus keyword matching and a bias toward the end of the log. It then sends those blocks to an LLM in one structured prompt, which returns the key error lines and up to three likely root causes.
- **Stage 2 (solution and execution):** it searches a company knowledge base of 1,206 internal documents and 23,344 past on-call Q&A pairs using 8 parallel search routes, writes a fix suggestion that cites its sources, and lets the LLM pick one of 4 repair tools. A human clicks to run the tool, and the pipeline re-runs automatically.

On 250 public GitHub Actions failures, Stage 1 reaches about 0.98–0.99 F1 with one LLM call and about 18k tokens. In one year at ByteDance it handled 1.07 M failures for 36,845 developers.

---

## 2. Problem they address

- CI/CD logs are **file-level, heterogeneous and noisy**. Many `WARNING`/`ERROR` lines are not the real cause, and logs are often longer than the model's context window.
- Earlier log-anomaly methods (DeepLog, LogAnomaly, LogPrompt, LogGPT) target *streaming system logs* with stable templates. They need training data or many sliding-window calls, and they do not produce fixes.
- Company knowledge about past fixes exists but sits in unstructured documents and chat Q&A, so it is rarely reused.

---

## 3. Method (what they actually built)

### 3.1 Offline preparation
| Step | Detail |
|---|---|
| Templates from successful runs | Run the **Drain** template parser over the **k = 3 most recent successful logs** of each pipeline. Store the resulting templates. |
| Knowledge base | 1,206 internal Feishu documents, split into chunks of up to 3,000 tokens and embedded. Plus 23,344 on-call Q&A pairs stored as-is. Copied into both a vector database (VikingDB) and Elasticsearch for failover. |

### 3.2 Stage 1: root-cause analysis
1. **Key log filtering** (Algorithm 1). A line from the failed log becomes a candidate if:
   - its template is **not** among the success-run templates (log diff, the most important rule), **or**
   - it contains a failure keyword: `fatal, fail, panic, error, exit, kill, no such file, err:, err!, failures:, err, missing, exception, cannot`, **or**
   - it is near the end of the log.
2. **Key log expansion.** Add **4 lines before and 6 lines after** each candidate, and merge blocks that overlap.
3. **Token overflow pruning.** Each line gets a weight:
   - The base weight is 3 if candidates are sparse (candidates ≤ 70 % of all lines and ≤ 500 lines), 1 otherwise, and 0 for non-candidates.
   - Explicit failure markers such as `--- FAIL:` get weight 10. Keywords and section headers add 2. Other candidates add 1.
   - Blocks are ranked by weight per line ("density") and added greedily up to a **22,000-token limit**.
4. **Prompt** (Fig. 2 in the paper): a role description, task-type identification, up to 10 key error lines given as line ranges, and at most 3 root causes with likelihood tags. The prompt explicitly forbids fix suggestions at this stage. Markdown output, not JSON.

### 3.3 Stage 2: solution and execution
- **Search query** = the LLM's root-cause sentence plus the critical log snippet (a hybrid query).
- **8 search routes:** 2 databases (VikingDB/Elasticsearch) × 2 match targets (query-to-document / query-to-title) × keyword (BM25) or vector (KNN) search, plus a wiki search API and a lookup in historical query logs.
- **Reranking** with BGE embeddings; the top 10 results are kept.
- **URL placeholders:** links are replaced with `files_0`, `files_1`, … before the LLM sees them and mapped back afterwards, so the model cannot invent URLs.
- **Solution prompt** (Fig. 3): every suggestion must cite a retrieved document, must be specific down to the command level, and must not give vague advice.
- **Tool calling**, kept separate from solution writing. The 4 tools are `lint_fix`, `image_fix`, `clean_cache` and `apply_resource`. The tool is shown to the user as a card; **a human clicks to execute**, and then the pipeline re-runs automatically.

---

## 4. Requirements and test data

| Item | Value |
|---|---|
| Public dataset | **367 failure cases from 76 GitHub repositories**, taken from the top 1,000 repositories by stars. For each, they downloaded the latest 300 workflow runs and paired a failed run with a successful run of the same workflow. Collected up to 25 Apr 2025. |
| Labels | Each case was **manually annotated with the key failing log lines and the root cause**. There are no fix labels and no failure-category taxonomy. |
| Split | **117 cases** used to build the Drain templates and the few-shot example pool. **250 cases** used for testing. |
| Models | GPT-4o, Claude-3.7-Sonnet and DeepSeek-V3 at temperature 0.1, all through cloud APIs. |
| Baselines | **LogPrompt** (20 examples in the prompt, window of 100 lines) and **LogGPT** (5 examples, window of 30). Both are general log-anomaly detectors, not CI/CD root-cause tools. |
| Industrial requirements | Several successful runs per pipeline (for the log diff), a large internal knowledge base, a vector database and search stack, internal repair tools, and a CI platform user interface. |

### Metrics
- A **true positive** for LogSage means its key lines overlap the ground-truth lines by **≥ 90 %**. For the baselines, the detection only has to fall inside a context window.
- A **false negative** for LogSage means it produced *no output at all*.
- Statistical test: Wilcoxon signed-rank, p < 0.001.
- Cost: average tokens and number of LLM calls per case.
- Online evaluation:
  - **150 random cases per week**, scored by hand for 10 weeks (Aug–Oct 2024).
  - Solutions are scored 0, 1 or 2 against a rubric, minus 1 point for a broken link.
  - Tool coverage rate and re-run success rate come from system logs.
  - A user survey (n = 191).

---

## 5. Findings and outcomes

| Result | Number |
|---|---|
| Stage-1 F1, offline (GPT-4o / Claude-3.7 / DeepSeek-V3) | 0.990 / 0.988 / 0.990 (precision ≈ 0.98, recall ≈ 0.99–1.0) |
| Best baseline F1 | 0.60 (LogPrompt with chain-of-thought on GPT-4o). LogGPT reaches 0.43–0.55. |
| Average tokens per case | **17,853** for LogSage, against 152,615 (LogPrompt) and 122,353 (LogGPT) |
| Average LLM calls per case | **1.0** for LogSage, against 31.7 and 89.4 |
| Online root-cause accuracy | > 85 % (human-scored) |
| Online end-to-end accuracy (solution quality) | > 80 % (human-scored) |
| Tool coverage | **22.4 %** on average (median 16.7 %) of cases that reached Stage 2 |
| Re-run success when a tool was used | **47.4 %** on average (range 36.8–60 %) |
| Scale | 1,070,613 failures, 36,845 developers, more than 5,000 weekly active users, more than 80 % coverage since Oct 2024 |
| User satisfaction | 7.97 / 10 |

**What the headline numbers really mean:** the "98 % precision" measures **locating the key error lines**, not classifying the failure and not fixing it. Truly automatic repair happens in about **0.224 × 0.474 ≈ 10.6 %** of the failures that reach Stage 2. For about 80 % of failures, LogSage gives advice, not an automatic fix.

---

## 6. Drawbacks and threats to validity

1. **The metrics favour LogSage.** The true-positive definition differs between LogSage (≥ 90 % overlap) and the baselines (inside a window). A false negative for LogSage only means "no output", so recall is close to 100 % by construction.
2. **Weak baselines.** LogPrompt and LogGPT are streaming anomaly detectors run in sliding windows, which is not their intended setting. There is no comparison with a simple "keyword + last N lines + same prompt" baseline or with RCACopilot-style retrieval.
3. **No ablation.** The paper never shows how much the log diff, the expansion and the pruning each contribute. The parameters (k = 3, 4 lines before / 6 after, 22k tokens, α, β, γ) are tuned "empirically" on internal data.
4. **Stage 2 is not reproducible.** The repair stage depends on the private knowledge base and tools and is evaluated only on internal data, by human scoring, during 10 weeks of a one-year deployment. There is no public benchmark for repair.
5. **"End-to-end precision > 80 %" is a human quality score, not a measured fix rate.** The measured fix rate (47 % of the 22 % of cases where a tool applies) is much lower.
6. **No failure taxonomy.** The paper gives no per-category accuracy, so we cannot tell which kinds of failure are hard.
7. **The log diff assumes repeated, stable runs.** Brand-new pipelines, rarely run pipelines, or pipelines with nondeterministic output lose the main filter.
8. **Logs are the only evidence.** Configuration files are mentioned in the prompt, but nothing uses cluster state, events, metrics or external services.
9. **No security or confidentiality model.** Logs go to cloud LLM APIs. Logs are not treated as untrusted input (prompt injection). The knowledge base could leak internal information.
10. **The tool set is tiny and fixed** (4 tools). Fixes are actions on the platform, not code or config patches, and nothing is reviewed through a PR or Git workflow.
11. **No latency reported.** The paper reports tokens and number of calls, but not wall-clock time from failure to fix suggestion.
12. **Hallucination risk is acknowledged but not measured** (Section VI).

---

## 7. Future work

**What the authors propose:**
- An autonomous agent with iterative reasoning in place of one-shot prompting.
- Fault prediction and anomaly prevention.
- Integration with observability tools and modelling of failure trends.

**What else follows from the gaps above:**
- An ablation of each preprocessing step, and metrics that treat all methods the same way.
- A benchmark for the *repair* stage with fix labels and re-execution (CI-Repair-Bench, arXiv:2604.27148, starts this).
- Evidence beyond logs: platform state, configuration, attestation services.
- Repair as patches in Git with review, instead of opaque platform tools.
- Diagnosis inside a TEE so that sensitive logs never leave protected memory.
- Per-category accuracy and confusion analysis.

**Our thesis covers four of these:** evidence beyond logs, patches through Git PRs, inference inside a TEE, and per-category analysis.

---

## 8. Is it relevant for our thesis?

**Yes, highly relevant, but as a *method and baseline* paper, not as a direct solution.**

| Our focus area / RQ | What LogSage gives us | What it does not cover |
|---|---|---|
| 01 Kubernetes evidence collection / **RQ1** | A tested way to cut container logs down to a short, high-signal input under a fixed token budget (log diff + keywords + log tail + 4/6 context + density pruning). | Kubernetes events, pod status, SGX node state, Argo step status. |
| 02 Attestation diagnostics | Nothing directly. **This is our novelty.** | SCONE, LAS, CAS, MRENCLAVE, SGX. |
| 03 Prompt engineering / **RQ2** | A ready template: role + task type + key lines as ranges + ≤ 3 causes with likelihood + "no fixes at this stage". A clean split between diagnosis and repair prompts. | No comparison of prompt formats. JSON, retrieval and example variants are not ablated, so **our RQ2 can fill that gap.** |
| 04 Classification and patching | Separating "reasoning" from "action" (tool calling). Search with root cause + log snippet. URL placeholders against hallucinated links; apply the same idea to MRENCLAVE hashes and session names. | No taxonomy; fixes are platform actions, not file patches. |
| 05 Automated PR generation | The human confirmation step + automatic re-run is the same pattern as our PR review + Argo re-trigger. | No Git or PR workflow. |
| 06 Recurrence tracking | Drain templates and searching past on-call Q&A are the basis for detecting recurring signatures and promoting them (≥ 3×). | Nothing promotes patterns to shared templates. |
| **RQ3** accuracy | A baseline to compare against. | Their metric is line localisation; ours must be **per-class classification accuracy**. |
| **RQ4** latency | Token and call-count figures (~18k tokens, 1 call) are useful when sizing a model running inside an enclave. | No wall-clock latency; no TEE overhead. |
| **RQ5** effectiveness | Two concrete metrics to reuse: **tool coverage rate** and **re-run success rate**. Their ~47 % gives a realistic reference point. | Not measured on a public dataset. |

### Corrections for our slides
- The "98 % precision" in our slide 4 should read **"98 % precision in locating key log lines on 250 GitHub Actions failures"**. Next to it, state the measured auto-fix figures: 22 % tool coverage and 47 % re-run success.
- "1M+ runs" should read "1.07 M failed executions processed".
- **Calling it a "primary baseline" is feasible but needs care:**
  - The dataset is public, but **no code is released**, so we must re-implement Stage 1 from the paper. That is possible because all the parameters are given.
  - Stage 2 cannot be reproduced; use our own template-based patcher instead.

---

## 9. How to use LogSage in our system (concrete plan)

1. **Re-implement LogSage Stage 1 as our "LogSage-K8s" baseline.**
   - Input: `kubectl logs --previous` plus the Argo step logs.
   - Learn Drain templates from the last 3 successful runs of the same workflow or image.
   - Use the same weights, the same 4/6 expansion, and a token budget matched to the in-enclave model.
2. **Extend the keyword list with SCONE/SGX terms**, for example:
   - `MRENCLAVE`, `attestation`, `CAS`, `session`, `predecessor`, `SCONE_`, `enclave`, `EPC`, `sgx`, `LAS`, `quote`, `TCB`, `permission denied`, `secret`.
   - Give explicit SCONE fatal markers weight 10, matching their `--- FAIL:` rule.
3. **Add evidence that is not in the logs.** This is our main addition, and it is what LogSage cannot see:
   - Kubernetes events and pod status (`OOMKilled`, `Pending` reasons);
   - SGX device-plugin capacity;
   - CAS session state;
   - LAS/PCCS health.

   Put these into one structured evidence bundle.
4. **RQ2 experiment design.** Compare the following, and report per-class F1 plus tokens and latency:
   - (a) raw log tail;
   - (b) LogSage-style filtered log blocks with the markdown prompt;
   - (c) (b) + structured Kubernetes/SCONE evidence as JSON;
   - (d) (c) + retrieval over past fixes;
   - (e) (d) + few-shot examples.
5. **Copy the two-stage separation.** First diagnose into a fixed label set with evidence lines. Then fill in a patch template for that label. Use the placeholder trick: the LLM refers to `HASH_0` / `SESSION_0` and the system fills in values computed deterministically.
6. **Adopt their metrics so results are comparable.**
   - Line-localisation P/R/F1, as in LogSage.
   - Per-class accuracy, which LogSage lacks.
   - Tool or patch coverage rate and re-run success rate (RQ5).
   - Tokens and number of calls, plus wall-clock latency (RQ4).
7. **Address their weaknesses explicitly** in the threats-to-validity section:
   - the same true-positive definition for every method;
   - an ablation of each evidence source;
   - a labelled fault-injection dataset with ground-truth fixes;
   - measured re-execution success instead of human-rated "precision".

---

## 10. Verdict

| Question | Answer |
|---|---|
| Relevant? | **Yes.** It is the closest published end-to-end LLM system for CI/CD failure diagnosis and repair, with industrial evidence. |
| Directly solves our problem? | **No.** It has no Kubernetes state, no SGX/CAS/attestation, no TEE, and no Git/PR repair. |
| Best use | (1) A re-implemented Stage-1 baseline for RQ3. (2) A design template for log preprocessing (RQ1) and prompts (RQ2). (3) A source of metrics for RQ4 and RQ5. (4) Its limits, as stated above, support our research gap. |
| Risk | Over-quoting "98 %". Always say what that number measures. |
