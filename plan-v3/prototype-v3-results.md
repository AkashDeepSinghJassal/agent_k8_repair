# Prototype v3 live demo results (up to human approval)

Date: 2026-10-28. Branch prototype-v3. Agent provider: claude-cli (sonnet), analyzer mode agent, no fallback used (fallbackFrom absent in both).
Merge and verify are pending the human. Both PRs are left open.

## F4 (api, DB_URL removed from api-config) - ri-ef9d6f8d30
- Detect to PR: 22:45:17Z -> 22:46:41Z (UTC) = 84 s (collected 22:45:39, proposed 22:46:29)
- Agent: 10 steps; tools: k8s_describe_pod, k8s_logs, git_read, k8s_get, git_list, git_read, git_log, git_diff, propose_patch
- Class CONFIG_ERROR, confidence 0.93, tier 3, attempts 1
- Proposal: deploy/demo/configmaps.yaml, add `DB_URL: redis://redis:6379` under api-config data (value found in git history, commit 30b0d078)
- PR: https://github.com/AkashDeepSinghJassal/repair-demo-app/pull/27 (branch repair/ri-ef9d6f8d30; the branch and PR from an earlier Oct 23 run were reused and updated, not a new PR)
- Phase: AwaitingReview

## F10 (api Service renamed to api-svc, frontend nginx crashes) - ri-2c6b41c60b-2
- Detect to proposal: 23:45:30Z -> 23:46:53Z = 83 s. PR opened 23:52:07Z (6 min 37 s after detection) because I restarted the operator between proposal and PR (see below); an undisturbed run would be about 85-90 s.
- Agent: 11 steps; tools: k8s_describe_pod, k8s_logs, git_read, k8s_list, git_list, k8s_logs, git_read, git_read, k8s_list, propose_patch
- Class CONFIG_ERROR, confidence 0.85, tier 3, attempts 1 (earlier attempts on ri-2c6b41c60b were escalated, see bugs)
- Proposal: deploy/demo/configmaps.yaml, `proxy_pass http://api:8000/;` -> `proxy_pass http://api-svc:8000/;`
- PR: https://github.com/AkashDeepSinghJassal/repair-demo-app/pull/28
- Phase: AwaitingReview

## Problems found
1. Stale operator and dashboard from Friday (pids 13380-13384) were still running against the same cluster. start_demo.sh only checks its own pid files, so two operators raced. One of them (llm-first, groq) handled the first F10 incident and escalated it as not_fixable_in_git. Killed them. Not fixed in the script.
2. A stale AwaitingReview incident for F4 (PR 27) blocked a new api incident through the fingerprint dedupe; deleted it and the same incident name and PR were reused.
3. Guardrail false positive (fixed): grounded_values rejected the correct F10 fix three times (attempts exhausted, Escalated, max_attempts) because the recomposed URL `http://api-svc:8000/;` appeared nowhere verbatim, although host api-svc and port 8000 were both in the evidence. Fix: src/repair_operator/guardrails.py:142-154 (_components_grounded) and its use at :162; test added in tests/operator/test_guardrails.py. 365 tests pass, ruff clean. Invented hosts (postgres://example, other:8000) are still rejected.
4. Restarting the operator left the old process hanging in shutdown for a few minutes; it was gone by the end.
