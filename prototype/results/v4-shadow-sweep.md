# v4 shadow sweep (issue #28, phase A)

Branch `prototype-v4`. Demo in shadow mode (`REPAIR_SHADOW_MODE=1`), `REPAIR_ANALYZER_MODE=hybrid`, `REPAIR_AGENT_PROVIDER=claude-cli`. Cluster `kind-repair`, demo repo `AkashDeepSinghJassal/repair-demo-app`. Faults run one at a time in the order F4, F1, F2, F3, F5, F6, F8, F9, F10, F11, F12, F14, F15, F16, F17, F18, F20, F21, F22, F23, F24, F25, F26, F27, F13, F19 (F7 skipped, it taints the node).

Start: `start_demo.sh` reports the LLM fallback provider as `groq` (because `GROQ_API_KEY` is set). The agent provider is `claude-cli`.

Pre-run state: `active-faults.json` still listed a stale F1 entry for `demo` from an earlier session. The repo's `api.yaml` was at baseline, so this was not a live fault. Pre-existing RepairIncidents `ri-2c6b41c60b-2`, `ri-6d7ca9f087`, `ri-ef9d6f8d30` (all Healed, from earlier runs) were left in place.

## Results

| # | Fault | Detected | Class | Tier | Tool calls | Tokens (in/out) | File | Edit | Root cause | Fix correct | Milestones | Guardrails | Detect to proposal | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | F4 DB_URL removed | no (8 min) | - | - | - | - | - | - | - | n/a | 0/3 (no incident) | - | - | Injected commit f8fd55f, Argo synced. api pod was not restarted (23m old), so the missing key never reached the running container. Catalog has no restart_target for F4. Reverted in d88212d. |
| 2 | F1 api memory 32Mi | late (incident created 15:58:21Z, injected 15:48:11Z, about 10 min) | OOM_KILLED | T3 (no playbook yet) | 10 across 2 runs (5 per run) | run 1: 35,024 in / 1,253 out ($0.058); run 2: 35,262 in / 1,201 out ($0.044) | deploy/demo/api.yaml | memory 24Mi->96Mi and 32Mi->128Mi | commit 81278dd lowered api limit 128Mi->32Mi (OOMKilled 137) | yes | 3/3 (OOMKilled 137 and limit 32Mi from describe_pod and git_read; diff shows 128Mi->32Mi) | shadow, no PR | 33 min (detected 15:58:21Z, proposal 16:32:27Z) | Phase Escalated (reason shadow_mode). Two T3 runs in one incident (see Bugs). Detection was about 10 min after injection, past the 8 min window, so the fault stayed in place until the incident finished. Reverted in f08a4e4. Incident ri-6d7ca9f087-2 deleted. |

## Bugs

- **B1 shadow-mode escalation emits no `outcome` event, and the incident re-runs the agent.** F1 incident `ri-6d7ca9f087-2`: `repair_flow.py:231-235` sets the phase to Escalated (reason `shadow_mode`), but the flow file has no `outcome` event. After the first proposal, `learn` skipped and the route/agent ran again: 2 `agent_start`, 2 `agent_end`, 2 `proposal`, both with no `outcome`. Each run costs about $0.05. Likely causes: the handler re-runs after the terminal status change, and `incidents.py:205-208` only emits `outcome` on some paths. Needs checking.
- **B2 detection to evidence gap of 33 min with no events.** F1: detected 15:58:21Z, evidence 16:31:48Z. The pod was created 15:48:23Z, so the first detection came about 10 min after the fault. The trace has nothing in between, so the cause is not visible. Possibly the operator stalled or the debounce did not run. Check `operator.log`.
- **B3 F4 cannot be detected with the current catalog.** The F4 ConfigMap change does not restart the api pod, and the entry has no `restart_target`. The running container never sees the missing DB_URL, so no incident fires (8 min, api pod 23m old). The F4 catalog entry or the injector needs a restart step, as F10/F11 have.

## Observations

## Per-fault notes
