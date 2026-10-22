# Prototype Contracts (read before any task)

Single source of truth for names, layout and interfaces shared between tasks. Design sources, in priority order:
1. `../repair-loop-prototype-implementation-plan.md` (the prototype plan);
2. `../confidential-repair-loop-blueprint.md`;
3. `../plan-v2-comparison.md` §8, for the plan v2 additions.

If this file conflicts with them, **this file wins**. If you need to change a contract, say so in your final report; don't silently diverge.

## 1. Repos and branches

| Repo | Purpose | Who writes |
|---|---|---|
| `AkashDeepSinghJassal/agent_k8_repair`, branch `prototype` | Operator, dashboard, fault tooling, cluster bootstrap, policies, tests | Task agents (via task branches) |
| `AkashDeepSinghJassal/repair-demo-app` (**public**) | GitOps target: `deploy/` manifests, `workflows/` templates. Argo CD syncs `deploy/` | Task #2 creates it; afterwards only the operator (PRs) and the fault injector (commits) |

- Task branch name: `task/<issue-number>-<slug>`, branched from `prototype`. Commit messages end with `Refs #<issue>` and the trailer `Co-Authored-By: Claude <noreply@anthropic.com>`. Push the task branch to `origin`. **Never push to `prototype`, `plan-*` or `main`**; only the merge step does that.
- Never commit secrets. Tokens come from environment variables only.

## 2. Layout of `prototype/`

```
prototype/
  CONTRACTS.md
  Makefile                 # targets: install, test, lint, cluster-up, cluster-down, operator, dashboard
  pyproject.toml           # ONE uv project, python >=3.11, src layout
  src/
    repair_operator/       # Kopf operator (#4 core, #6 evidence, #7 analyzer, #8 guardrails, #10 PR, #11 verify, #13 metrics)
    repair_dashboard/      # FastAPI + static UI (#5, #12)
    repair_faults/         # fault catalog + injector (#9)
  cluster/                 # kind config, helm values, bootstrap, Argo CD Applications (#3)
  crd/repairincident.yaml  # CRD (#4)
  schemas/evidence.v1.json # (#6)
  policy/                  # conftest Rego (#8)
  tests/                   # pytest, mirrors src/ (tests/operator, tests/dashboard, tests/faults)
```

Dependencies (all declared in `pyproject.toml` by #1; add a new one only if your task needs it, and mention it in the report):
`kopf, kubernetes, PyGithub, pydantic>=2, jsonschema, anthropic, fastapi, uvicorn[standard], pyyaml, httpx`; dev: `pytest, pytest-asyncio, ruff`.

Commands: `cd prototype && uv sync`, `uv run pytest -q`, `uv run ruff check src tests`. **Unit tests must not need a cluster, GitHub or the Anthropic API**: use fakes and mocks. Mark live tests with `@pytest.mark.live`; they are skipped by default (configure in `pyproject.toml`).

## 3. Runtime environment

| Thing | Value |
|---|---|
| Cluster | kind cluster `repair`, kube context `kind-repair`, one node |
| Namespaces | `demo`, `demo-b` (app), `argocd`, `argo` (Workflows), `repair-system` (operator CRs live in the app namespaces) |
| Demo frontend | NodePort 30080 → host `localhost:8080` (kind `extraPortMappings`) |
| Argo CD UI | `kubectl -n argocd port-forward svc/argocd-server 8443:443` |
| Argo Workflows UI | `kubectl -n argo port-forward svc/argo-workflows-server 2746:2746` |
| Operator | runs **locally**: `uv run kopf run -m repair_operator.main --all-namespaces` (containerised later) |
| Dashboard | runs **locally**: `uv run uvicorn repair_dashboard.app:app --port 8090` |
| Argo CD Applications | `demo-app` → `deploy/demo`, ns `demo`; `demo-app-b` → `deploy/demo-b`, ns `demo-b`; automated sync, prune, selfHeal |

Environment variables (read through `repair_operator/config.py` → `Settings`, a pydantic model with these defaults):

| Var | Default | Meaning |
|---|---|---|
| `REPAIR_GITHUB_TOKEN` | (none) | Fine-grained PAT for `repair-demo-app` (contents + pull requests RW) |
| `REPAIR_DEMO_REPO` | `AkashDeepSinghJassal/repair-demo-app` | |
| `REPAIR_DEMO_BRANCH` | `main` | |
| `ANTHROPIC_API_KEY` | (from env) | |
| `REPAIR_LLM_MODEL` | `claude-sonnet-5-5` | No temperature/top_p/prefill/forced tool_choice |
| `REPAIR_ANALYZER_MODE` | `llm-first` | `llm-first`: LLM always diagnoses (rule table is a hint, disagreement recorded); `rules-first`: tier 1 rules+template before the LLM |
| `REPAIR_SHADOW_MODE` | `false` | L0: classify + log, no PR |
| `REPAIR_DEBOUNCE_SECONDS` | `60` | |
| `REPAIR_VERIFY_WINDOW_SECONDS` | `120` | Demo-friendly (plan says 600) |
| `REPAIR_MAX_ATTEMPTS` | `3` | |
| `REPAIR_COOLDOWN_SECONDS` | `600` | |
| `REPAIR_STATE_DIR` | `~/.repair-loop` | SQLite recipe store + evidence/prompt replay files |
| `REPAIR_PROMOTION_THRESHOLD` | `3` | Verified fixes before tier-0 promotion |

## 4. CRD `RepairIncident`

`repair.example.dev/v1alpha1`, kind `RepairIncident`, plural `repairincidents`, short name `ri`, namespaced, status subresource; columns Phase, Class, Tier, PR. `spec`: `source` (object), `fingerprint` (string). `status` (preserve unknown fields) contains:

```
phase: Detected|Collected|Dismissed|Analyzing|Proposed|AwaitingReview|Merged|Verifying|Healed|Failed|Escalated
attempts: int
signature: str                 # knowledge fingerprint (plan v2)
evidenceRef: str               # "sha256:<hex>"; bundle stored outside the CR
classification: {class, confidence, source: rules|llm|recipe}
proposal: {tier: 0|1|2|3|4, file, edits:[{find,replace}], template?, params?, rationale, needs_remeasure: bool, rejectedBy?: str}
pullRequest: str (url)
mergeSha: str                  # merge commit; set when the PR merged (Merged phase)
reviewCursor: {...}            # which PR reviews/comments were already consumed (no double triggers)
approvedBy: str                # reviewer who approved / merged
reviewFeedback: [str]
verify: {startedAt, healthySince, rerun, restarts}
                               # startedAt: ISO time Verifying began; healthySince: start of the current
                               # healthy stretch (null resets it); rerun: name of the resubmitted workflow
                               # (workflow incidents); restarts: {podName: total restartCount} baselines
learning: {recorded: bool, outcome: healed|approved|failed, mergedMatchesProposal: bool|null}
outcome: {result: healed|failed|escalated|dismissed, reason?: str}
shadow: bool
timestamps: {detected, collected, analyzed, proposed, prOpened, merged, synced, verified, closed}
```

Notes: `analyzer.analyze` raises `InvalidOutput` when the LLM output is unusable after its internal repair attempt; `repair_flow.analyze_step` retries (each retry consumes an attempt) and escalates once `attempts > max_attempts` (the same `>` comparison on the verify-failure path). Shadow mode (`REPAIR_SHADOW_MODE`) analyses but never opens a PR: the incident ends in `Escalated` with `outcome.reason: shadow_mode` and `shadow: true`. A verify window judges all current pods of the owner (Ready, no waiting error, unchanged restart counts, Application Healthy), so ConfigMap-only fixes work without recreated pods.

Labels: `repair.example.dev/ignore: "true"` excludes a pod or workload from detection. `repair.example.dev/incident: <name>` marks a workflow resubmitted by the operator.

Manifest annotation in `repair-demo-app`: `repair.example.dev/source-path: deploy/demo/api.yaml`. Every workload carries the path of its own file.

## 5. Two fingerprints (plan v2 §6), in `repair_operator/fingerprint.py`

- `dedupe_fingerprint(namespace, workload, container_or_step, reason) -> str`: sha256 hex. Incident name = `ri-` + first 10 hex characters.
- `knowledge_signature(reason, exit_code, step_type, error_lines, extra_codes=()) -> str`: normalise each line (timestamps → `<TS>`, hex ≥12 → `<HASH>`, IPv4[:port] → `<IP>`, pod names `-<hash>-<5char>` → `<POD>`, numbers ≥4 digits → `<N>`, quoted names → `<NAME>`), dedupe, sort, then sha256 of `reason|exit_code|step_type|lines...|codes`. The first 12 hex characters are the signature. **No namespace or workload name.**

## 6. Evidence bundle (`schemas/evidence.v1.json`)

The shape in the prototype plan § "Evidence bundle", plus `"signature"`. Size caps: 20 events, 200 log lines per stream, 400 lines of file. Redaction before storage. Stored as `REPAIR_STATE_DIR/evidence/<incident>-<attempt>.json`.

## 7. Analyzer contract (`repair_operator/analyzer/`)

Taxonomy (prototype): `OOM_KILLED, IMAGE_PULL, CONFIG_ERROR, CRASH_LOOP, STAGE_FAILURE, UNSCHEDULABLE`.

Tiers:

| Tier | Source | Classes |
|---|---|---|
| 0 | Promoted recipe matching `signature`, no LLM | any with a template |
| 1 | Rule table + deterministic template (`rules-first` mode only) | OOM_KILLED, IMAGE_PULL, CONFIG_ERROR |
| 2 | LLM picks template + params (always, in `llm-first`) | same three |
| 3 | LLM `find/replace` edits | CRASH_LOOP, STAGE_FAILURE |
| 4 | Escalate, no PR | UNSCHEDULABLE, `fixable_in_git=false`, low confidence |

Public interface (pydantic models in `repair_operator/models.py`):

```python
class Edit(BaseModel): find: str; replace: str
class Proposal(BaseModel):
    cls: str = Field(alias="class"); root_cause: str; confidence: float
    fixable_in_git: bool; file: str | None; edits: list[Edit] = []
    template: str | None = None; params: dict = {}
    rationale: str = ""; verify: str = ""; needs_remeasure: bool = False
    tier: int; source: Literal["rules","llm","recipe"]
def analyze(bundle: dict, feedback: list[str] = []) -> Proposal   # the ONLY entry point the operator calls
```

LLM call (`analyzer/llm.py`): Anthropic Python SDK, `client.messages.parse(model=settings.llm_model, max_tokens=4096, system=[{"type":"text","text":SYSTEM_PROMPT,"cache_control":{"type":"ephemeral"}}], messages=[...], output_format=LlmProposal)`, then read `response.parsed_output`. Check `response.stop_reason == "refusal"` first and treat it as `invalid_output`. Evidence goes inside `<evidence>…</evidence>`, which the prompt declares untrusted. Store the prompt and reply under `REPAIR_STATE_DIR/replay/`.

## 8. Guardrails (`repair_operator/guardrails.py`)

`check(proposal, current_text: str, new_text: str, path: str) -> GuardrailResult(ok: bool, rejected_by: str|None, details: str)`. Checks run in order: `path_allowlist, exact_edit, size_cap(10), static(yamllint+kubeconform -strict / argo lint), server_dry_run, policy(conftest policy/)`. Each check is a function that can be skipped by name in tests (the cluster-dependent `server_dry_run` is skipped when no cluster is reachable).

## 9. Recipe store (`repair_operator/knowledge.py`)

SQLite at `REPAIR_STATE_DIR/knowledge.db`, table `recipes(signature PK, class, template, params_json, status candidate|verified|promoted|demoted, approved, rerun_passed, rejected, updated_at)`. API: `lookup(signature) -> Recipe|None` (returns only `promoted`), `record_outcome(signature, cls, template, params, outcome)` (promotion when `rerun_passed >= threshold`; any failure or rejection after promotion → `demoted`), `similar(signature) -> list` (stub for now).

## 10. Fault catalog (`repair_faults/catalog.yaml`)

Entries F1–F9: `id, title, description, namespace, target_file, kind: git|cluster|workflow, breaking_edits: [{find, replace}], expected_class, expected_outcome: Healed|Escalated|Blocked, correct_fix`. API in `repair_faults/injector.py`: `list_faults()`, `inject(fault_id) -> dict` (commit to the demo repo, or a cluster action), `revert(fault_id) -> dict`, all through an injectable GitHub/Kube client so they can be tested.

## 11. Definition of done for every task

- Code under the paths above, `uv run pytest -q` green, `uv run ruff check` clean for the files you touched.
- A short report: what you built, deviations from the contracts, how you tested, anything left open.
