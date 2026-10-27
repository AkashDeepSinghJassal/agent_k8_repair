# Failure taxonomy for the repair loop

Purpose: say which Kubernetes/GitOps failure classes the fault set (`prototype/src/repair_faults/catalog.yaml`, F1-F16) covers, which it does not, and why. The operator only reacts to CrashLoopBackOff, ImagePullBackOff/ErrImagePull, CreateContainerConfigError, OOMKilled, FailedScheduling and failed Argo Workflows, so a class counts as "in scope" only if it ends in one of those symptoms.

"Where the cause lives" matters because the operator's evidence bundle is fixed: failing pod status/events/logs, the pod's own manifest file, and the ConfigMaps it references (with their last diff). A cause in any other file requires the agent to use the git/k8s/argo MCP tools.

## Taxonomy

| Class | Typical cause | Symptom the operator sees | Cause lives in | Fixable in git? | Faults |
|---|---|---|---|---|---|
| Memory limit too low | limit below working set | OOMKilled, exit 137 | same manifest | yes | F1, F9 |
| Bad image reference | typo/nonexistent tag, missing registry auth | ImagePullBackOff / ErrImagePull | same manifest | yes (tag); no (registry auth/outage) | F2 |
| Missing ConfigMap/Secret reference | name typo in `envFrom`/`valueFrom` | CreateContainerConfigError `configmap "x" not found` | same manifest | yes | F3 |
| Referenced object renamed/deleted | rename in the ConfigMap's own file, Deployment untouched | CreateContainerConfigError after restart | other resource (other file) | yes | F12 |
| Missing env var / config key | key dropped from ConfigMap | CrashLoopBackOff, app log "X not set" | other resource (ConfigMap file), log names the variable | yes | F4, F8 |
| ConfigMap key renamed under a subPath mount | key renamed, mount references old key | CrashLoopBackOff, runc "not a directory" | other resource | yes | F11 |
| Wrong command / script path | typo in `command`/`args` | CrashLoopBackOff, exec/no such file | same manifest | yes | F5 |
| Dependency name broken | Service renamed, upstream DNS name unresolved | CrashLoopBackOff (nginx "host not found in upstream") on restart | other resource (other file) | yes | F10 |
| Liveness probe misconfigured | wrong port/path | restarts then CrashLoopBackOff; app log clean; events show probe failures | same manifest | yes | F13 |
| securityContext conflict | `runAsNonRoot` with root image | CreateContainerConfigError "image will run as root" | same manifest | yes | F16 |
| Resource request exceeds node | typo/unit error in requests | FailedScheduling "Insufficient memory" (old pod keeps running during rollout) | same manifest | yes (history shows old value) | F14 |
| Storage unbound | PVC with unknown StorageClass | FailedScheduling "unbound immediate PersistentVolumeClaims" | other resource (PVC, cluster StorageClass) | yes | F15 |
| Node-level scheduling block | taint, cordon, no capacity | FailedScheduling / Pending | cluster | no (escalate) | F7 |
| Workflow stage failure | bad parameter/step input | Workflow Failed/Error | workflow template | yes | F6 |
| Prompt injection via repo/log content | untrusted text in source or logs | any of the above plus hostile instruction | app code | n/a (guardrail test) | F8 |
| Network policy blocks traffic | wrong selector/port | no pod failure; timeouts | other resource | yes | none, not detectable |
| Admission/quota/RBAC rejections | resource quota, webhook, missing CRD | Argo SyncFailed, no pod | cluster / Argo | partly | none, not detectable |
| Readiness-only failures, wrong Service selector/port | label or port mismatch | 0 endpoints, pods Running | other resource | yes | none, not detectable |
| App bug / dependency outage / secret rotation | code defect, external service down | CrashLoopBackOff but cause outside repo or cluster | app code / external | no (escalate) | none; F4-style crashes only cover config causes |
| Argo drift / sync-option errors | manual edits, immutable field changes | OutOfSync/SyncFailed | Argo / cluster | partly | none |

## Coverage of F1-F10 (original set)

F1-F3, F5 are single-file, single-manifest edits. F4/F8 are cross-resource only weakly (the ConfigMap is in the bundle). F6 is the workflow path, F7 the escalate path, F9 the second namespace, F10 the first genuine cross-file cause. Gaps before F11: only one cross-file cause, nothing where the referenced object disappeared (so the bundle cannot carry its diff), no probe/security/storage/scheduling-by-request classes, no fault whose log is misleadingly clean.

## Added faults (F11-F16)

All were injected and observed on the kind cluster (see the table; observed text is verbatim from the cluster).

| ID | Design | Cause is cross-resource? | Observed symptom | Outcome |
|---|---|---|---|---|
| F11 | frontend-nginx key `default.conf` -> `site.conf` in configmaps.yaml | yes, frontend.yaml unchanged; needs frontend pod restart | CrashLoopBackOff; `error mounting ".../volume-subpaths/nginx-conf/frontend/1" to rootfs at "/etc/nginx/conf.d/default.conf": ... not a directory` (class is CRASH_LOOP, not CONFIG_ERROR) | Healed |
| F12 | ConfigMap `api-b-config` renamed to `api-b-config-v2` in demo-b/configmaps.yaml | yes, and second namespace; old ConfigMap is pruned so no diff for it exists | CreateContainerConfigError, `Error: configmap "api-b-config" not found`, after pod restart | Healed |
| F13 | api-b livenessProbe port 8000 -> 8001 | no (same manifest), but log is clean | events `Liveness probe failed: ... :8001/healthz ... connection refused`; restarts at ~2 min; the pod shows CrashLoopBackOff only after ~7 min | Healed |
| F14 | worker requests.memory 64Gi, limits 96Gi (limits raised too because request > limit is rejected at admission) | no | new pod Pending, `FailedScheduling ... 1 Insufficient memory`; old pod keeps running | Healed (previous values in git history); escalation is the alternative if a policy forbids guessing sizes |
| F15 | redis.yaml gains PVC `redis-data` with `storageClassName: fast-ssd` and a volume mount | partly (PVC + cluster StorageClass) | new pod Pending, `FailedScheduling ... pod has unbound immediate PersistentVolumeClaims`; PVC event `storageclass ... "fast-ssd" not found`. Argo prunes the PVC on revert | Healed (change class to `standard` or drop the volume) |
| F16 | worker `securityContext.runAsNonRoot: true` | no | CreateContainerConfigError, `container has runAsNonRoot and image will run as root` | Healed |

Rejected or unsuitable candidates:

- Secret key renamed: the demo has no Secret and adding one adds nothing over F11/F12 (same symptom class).
- ConfigMap deleted or renamed for the volume-mounted `api-code`/`worker-code`: a missing ConfigMap volume gives ContainerCreating with FailedMount events, which no detector reacts to.
- Workflow wrong command/args: duplicates F6's STAGE_FAILURE path; a different cause location (Service rename) would only repeat F10.
- Request too large as an "escalate" case: the previous value exists in git, so a revert is a legitimate fix; F7 remains the escalate case.
- NetworkPolicy, wrong Service selector, readiness-only faults: pods stay Running, nothing for the operator to detect.

## Known limitation found during verification

Because ConfigMap-level faults (F11, F12) leave the running pod untouched, the failure only shows after a pod restart. The dashboard's `RESTART_TARGET` map now includes F11 (frontend) and F12 (api-b). F13 takes several minutes to reach CrashLoopBackOff.

## Sources

Read directly: none beyond search results; every source below was seen only as a search summary unless stated.

- Cloud-OpsBench, 452 fault cases / 40 root causes (v1; v2 reportedly 754 cases / 57 types, unverified): https://arxiv.org/abs/2603.00468 (search summary only)
- Mutiny! How does Kubernetes fail, and what can we do about it?: https://arxiv.org/pdf/2404.11169 (search summary only)
- SREGym, live benchmark of 90 SRE problems: https://arxiv.org/abs/2605.07161 (search summary only)
- AIOpsLab (fault library via ChaosMesh, DeathStarBench): https://arxiv.org/html/2501.06706v1 (search summary only)
- NetArena (network-policy YAML mutations): https://arxiv.org/pdf/2506.03231 (search summary only)
- Kubernetes Failure Stories (k8s.af): https://k8s.af/ and Zalando postmortems https://kubernetes-on-aws.readthedocs.io/en/stable/postmortems/index.html (search summary only)
- Vendor blogs on CrashLoopBackOff / ImagePullBackOff / OOMKilled causes: https://komodor.com/learn/how-to-fix-crashloopbackoff-in-kubernetes , https://thenewstack.io/top-10-kubernetes-deployment-errors-causes-and-fixes-and-tips/ (search summary only; the "80% of issues are misconfiguration" claim is unsourced and not used here)
- Argo CD sync/drift failure causes: https://oneuptime.com/blog/post/2026-02-26-argocd-sync-status-explained/view (search summary only)

The class table is my own synthesis of those summaries plus the observed cluster behaviour; it is not copied from any single catalog.
