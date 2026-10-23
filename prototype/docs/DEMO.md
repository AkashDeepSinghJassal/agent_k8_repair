# 10-minute demo script (supervisor presentation)

Goal: show that a broken deployment is diagnosed, fixed through a reviewed pull request and verified, with the AI never touching the cluster directly.

## Before the meeting (15 min ahead)

1. Docker running with 5-6 GB. `cd prototype && make install`.
2. `export REPAIR_GITHUB_TOKEN=...` and `export GROQ_API_KEY=...` (or `ANTHROPIC_API_KEY`).
3. `make demo-up` (creates the cluster if missing, starts operator and dashboard). Wait until http://localhost:8080 shows the demo app and http://localhost:8090 loads.
4. Check: all services green, Argo CD apps Synced/Healthy, no old incidents (`kubectl get ri -A`). Open tabs: the dashboard, the GitHub repo `repair-demo-app` (Pull requests), and a terminal running `tail -f ${TMPDIR:-/tmp}/repair-loop/operator.log`.
5. Dry-run F1 once, then revert it and delete the incident so the cooldown does not suppress the live run.

## Script

| Time | Say | Do / point at |
|---|---|---|
| 0:00-1:30 | "Thesis step 1: a Kubernetes-native repair loop outside the enclave. Detect, collect evidence, ask an LLM for a minimal fix, validate, open a PR, a human approves, Argo CD syncs, we verify. The AI has read-only access and can only open PRs." | Show the README architecture diagram, then the dashboard: Services green, Argo CD Synced/Healthy, demo app at :8080. |
| 1:30-2:30 | "The app is deployed by GitOps from this repo. Nothing is applied by hand." | GitHub tab: `deploy/demo/api.yaml`. Dashboard "Fault injection" section. |
| 2:30-3:30 | "I inject F1, a memory limit that is too low. This is a commit to the repo, like a bad release." | Click Inject on F1 and confirm. Point at the Argo CD sync, then the api service turning red (OOMKilled). |
| 3:30-5:00 | "The operator detects it, waits out the debounce, collects redacted evidence (pod status, events, logs, the manifest) and asks the model. With a 30 s debounce, detection to PR took about 60 s in the live runs (LLM call 2-3 s)." | Incidents table: `Detected` -> `Collected` -> `Analyzing` -> `Proposed` -> `AwaitingReview`. Show the operator log. |
| 5:00-6:30 | "Tier 2: the model picks a template and parameters, code produces the edit. Seven guardrails ran before the PR existed." | Click the incident row: class OOM_KILLED, tier, rationale, diff (`32Mi` -> `128Mi`). Open the PR on GitHub and show the same diff. |
| 6:30-7:30 | "A human decides. I approve." | Click Approve & merge in the drawer and confirm. Phase becomes `Merged`. If Argo is slow: `kubectl -n argocd annotate application demo-app argocd.argoproj.io/refresh=hard --overwrite`. |
| 7:30-8:30 | "Argo CD syncs the merge; the operator watches the pods for a verify window (120 s by default) before it declares success." | Argo CD panel syncing, service green, incident `Verifying` -> `Healed`. The drawer timestamps give detection-to-PR, merge and heal times. |
| 8:30-9:30 | **Safety point.** "The model can be wrong. Take F4, a removed `DB_URL`. Before related-file evidence existed the model invented `postgres://example`; now it sees the ConfigMap's history and the grounding guardrail rejects values that appear nowhere in the evidence. The guardrails cannot know the value is wrong; the reviewer can." | Inject F4 only if rehearsed. Show the PR with the invented value and reject it: Request changes in the dashboard, or comment `/repair retry use the redis service defined in the repo` on the PR. Show the incident return to `Analyzing` with the feedback and a new proposal. If short on time, describe it. Mention F8 (prompt injection inside the evidence) as the test that evidence is treated as untrusted data. |
| 9:30-10:00 | "Limits: F1-F3 and F5 are validated live (F4 wrong fix, redone with related-file evidence); it runs outside the enclave with a PAT. Next: more faults, compare with rule-only baselines, move the analyzer into SCONE." | README "Known limitations". |

## If something goes wrong

- Incident does not appear: wait out the debounce (60 s), check the operator log, check `kubectl get ri -A` for a cooldown.
- Phase `Escalated` and no PR: usually an LLM key or billing error. Show the log and explain that escalation is the safe default; switch `REPAIR_LLM_PROVIDER` for the next run.
- Argo is slow: hard refresh as above.
- Reset between runs: revert the fault in the dashboard, then `kubectl -n demo delete ri --all`.

## Questions for the supervisor

1. Is human approval on every PR the right default for the thesis, or should the evaluation include an auto-merge level for promoted recipes?
2. Which fault set and baselines (rule-only, LogSage-style, no-memory loop) are required before step 2 (moving into the enclave)?
3. How much weight should the safety evaluation (F8 injection, invented values, guardrail blocks) get relative to repair success rate?
4. Is injected-fault data enough for step 1, or should real failures from the SCONE setup be collected early?
5. Is a hosted open model (Groq) acceptable for the experiments, given reproducibility, or should results use a pinned Anthropic model?
