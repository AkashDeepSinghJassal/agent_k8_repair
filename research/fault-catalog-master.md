# Master fault catalog for the Kubernetes/GitOps repair loop

Purpose: one de-duplicated list of the faults that production incident studies and fault benchmarks report, scored for our thesis (an AI repair loop for Kubernetes/GitOps on a single kind cluster). It answers three questions for each fault: can our operator even see it, can the fix be a git change, and can we reproduce it in the demo without changing application code. Implemented faults are `F1`-`F27` in `prototype/src/repair_faults/catalog.yaml`; this list is the wider space they are drawn from (issue #27).

How to read the table:

- **operator detects**: what the operator's detectors react to (`prototype/src/repair_operator/detectors.py`): container waiting reasons `CrashLoopBackOff`, `ImagePullBackOff`/`ErrImagePull`, `CreateContainerConfigError`, last-terminated `OOMKilled`, Warning events `FailedScheduling`, and failed Argo Workflows. Anything else is `NOT detected`. Only `containerStatuses` are inspected, so init containers are not.
- **fix lives**: `same manifest` (the failing pod's own file), `other manifest` (another file in the demo repo), `git history` (revert a commit), `cluster-only` (no git change can fix it), `app code`, `hardware`.
- **fixable in git**: `yes`, `no`, or `escalate` (a human must act; the loop should open a ticket or PR comment, not guess).
- **demo-feasible**: reproducible on the kind cluster `kind-repair` (one node, Argo CD, Argo Workflows, demo app nginx/api/worker/redis in `demo`, api-b in `demo-b`) without changing application code. `yes (Fn)` means implemented and live-verified as that catalog entry; `yes` alone means feasible but not implemented; `needs-X` names what is missing.
- **sources**: SREGym = arXiv 2605.07161 (read in full, see `research/sregym-overview.md`); COB = Cloud-OpsBench (read in full, `research/cloud-opsbench-overview.md`); Gunawi, Liu19, ConfDef, Mutiny, Gray, Metastable, k8s.af and Argo practice are the production studies and practitioner material listed in the Sources section, **all seen only as search summaries** unless stated; `ours Fn` is a fault we implemented.
- **prio**: P1 = in the thesis fault set (implemented, detected, live-verified); P2 = worth adding or documenting as a gap; P3 = out of reach or out of scope for a single-node demo.

The sources are de-duplicated: one row per mechanism, even when SREGym, Cloud-OpsBench and a study each name it differently.


| id | fault | layer | typical symptom | operator detects | fix lives | fixable in git | demo-feasible on kind (no app-code change) | sources | prio |
|---|---|---|---|---|---|---|---|---|---|
| M001 | Required env var / config key missing from ConfigMap | config | app exits at start, log names the variable | CrashLoopBackOff | other manifest (ConfigMap file) | yes | yes (F4, F8) | SREGym missing env variable; COB missing parameter; ours F4/F8 | P1 |
| M002 | Explicit env entry shadows envFrom (empty or wrong value wins) | config | app exits or misbehaves although ConfigMap is correct | CrashLoopBackOff | same manifest | yes | yes (F21) | SREGym env variable shadowing; ConfDef 'configuration inexecutability'; ours F21 | P1 |
| M003 | envFrom/valueFrom points at a ConfigMap that does not exist (typo) | config | pod stuck, event 'configmap not found' | CreateContainerConfigError | same manifest | yes | yes (F3) | SREGym missing configmap; ours F3 | P1 |
| M004 | Referenced ConfigMap renamed or deleted in its own file | config | new pod stuck after restart, old pod fine | CreateContainerConfigError | other manifest (ConfigMap file); diff of the pruned object is gone | yes | yes (F12) | ConfDef; ours F12 | P1 |
| M005 | ConfigMap key renamed while a subPath mount still names the old key | config | restarted pod fails mounting 'not a directory' | CrashLoopBackOff | other manifest (ConfigMap file) | yes | yes (F11) | ours F11 | P1 |
| M006 | Secret exists but the referenced key is missing | config | pod stuck, event "couldn't find key X in Secret" | CreateContainerConfigError | same manifest (or secret store) | yes | yes (F18) | COB missing Secret; ours F18 | P1 |
| M007 | Referenced Secret does not exist | config | pod stuck, event 'secret not found' | CreateContainerConfigError | other manifest or secret store | escalate | yes | COB missing Secret; Argo practice (secrets not in git) | P2 |
| M008 | Secret rotated or credential expired, app auth to backend fails | config | app logs auth error; may crash or return 5xx | CrashLoopBackOff | secret store / backend, not git | no | needs-X (a backend with auth; redis requirepass is possible) | SREGym auth miss / revoke auth mongodb, valkey auth disruption; COB MySQL bad credentials | P2 |
| M009 | Wrong backend address in env (port or host typo) | config | connection errors; crash only if app exits on failure | NOT detected | same or other manifest | yes | needs-X (app that connects at start) | COB ServiceEnvVarAddressMismatch; SREGym incorrect port assignment | P2 |
| M010 | Invalid argument or flag to a third-party image (e.g. redis eviction policy) | config | process exits at start with parser error | CrashLoopBackOff | same manifest | yes | yes (F22) | COB; ours F22 | P1 |
| M011 | Wrong script path or typo in command | k8s workload | container exits, 'no such file' | CrashLoopBackOff | same manifest | yes | yes (F5) | SREGym wrong bin usage; ours F5 | P1 |
| M012 | Command binary not found in image (exec: not found) | k8s workload | container never starts, reason CreateContainerError/RunContainerError | NOT detected | same manifest | yes | yes (not live-tested) | SREGym wrong bin usage; detectors.py POD_REASONS lacks CreateContainerError | P2 |
| M013 | runAsNonRoot on an image that runs as root | k8s workload | pod stuck, 'image will run as root' | CreateContainerConfigError | same manifest | yes | yes (F16) | ConfDef; ours F16 | P1 |
| M014 | Volume mount permission / fsGroup / readOnlyRootFilesystem breaks a writing app | k8s workload | app exits with permission denied on write | CrashLoopBackOff | same manifest | yes | needs-X (app that writes to disk) | COB volume mount permission | P2 |
| M015 | Security context rejected by admission (privileged, capabilities, PSA) | control plane | ReplicaSet FailedCreate, no pod exists | NOT detected | same manifest or namespace label | yes | yes | SREGym security context fault; COB admission control | P2 |
| M016 | Image tag does not exist (typo) | k8s workload | ErrImagePull then ImagePullBackOff | ImagePull | same manifest | yes | yes (F2, F27) | COB IncorrectImageReference; SREGym incorrect image; ours F2/F27 | P1 |
| M017 | Private registry image without or with wrong pull secret | k8s workload | ImagePullBackOff, 'unauthorized' | ImagePull | registry credential, not git | escalate | yes (F23) | COB missing image pull secret; Argo practice; ours F23 | P1 |
| M018 | Registry unreachable, DNS failure or rate limit (Docker Hub 429) | k8s workload | ImagePullBackOff on many pods at once | ImagePull | external registry / cluster DNS | no | no (external) | COB registry DNS failure; k8s.af | P2 |
| M019 | Image built for the wrong CPU architecture | k8s workload | exec format error, CrashLoopBackOff | CrashLoopBackOff | same manifest (tag/digest) | yes | yes (pick an amd64-only tag; not live-tested) | practitioner reports (search summary) | P2 |
| M020 | Bad image release breaks several services at once (correlated) | k8s workload | multiple deployments crash after one shared tag bump | CrashLoopBackOff | git history (revert the bump) | yes | needs-X (second workload sharing the tag) | SREGym faulty image correlated, update incompatible correlated | P2 |
| M021 | Liveness probe on the wrong port | k8s workload | restarts, events 'connection refused', later CrashLoop; log clean | CrashLoopBackOff | same manifest | yes | yes (F13) | COB LivenessProbeIncorrectPort; SREGym liveness probe misconfiguration; ours F13 | P1 |
| M022 | Liveness probe on the wrong path (404) | k8s workload | restarts, events 'statuscode: 404'; log clean | CrashLoopBackOff | same manifest | yes | yes (F19) | SREGym liveness probe misconfiguration; ours F19 | P1 |
| M023 | Liveness probe too aggressive (timeout 1s, failureThreshold 1, no initial delay) | k8s workload | restarts on slow starts or short stalls | CrashLoopBackOff | same manifest | yes | yes (not implemented) | SREGym liveness probe too aggressive; COB probe timing | P2 |
| M024 | Readiness probe wrong port or path | k8s workload | pod Running, never Ready; rollout stalls; no endpoints | NOT detected | same manifest | yes | yes | COB; SREGym readiness probe misconfiguration, failed readiness probe | P2 |
| M025 | Startup probe too short for a slow app | k8s workload | kill loop during start | CrashLoopBackOff | same manifest | yes | needs-X (slow-starting app) | COB probe timing | P3 |
| M026 | Memory limit below working set | k8s workload | OOMKilled, exit 137 | OOMKilled | same manifest | yes | yes (F1, F9, F26) | COB OOMKilled; SREGym valkey memory disruption; ours F1/F9/F26 | P1 |
| M027 | Memory request larger than any node | scheduling | new pod Pending; old pod keeps running | FailedScheduling | same manifest (git history) | yes | yes (F14) | COB MemoryCapacityMismatch; SREGym resource request too large; ours F14 | P1 |
| M028 | CPU request larger than any node | scheduling | new pod Pending, 'Insufficient cpu' | FailedScheduling | same manifest | yes | yes | COB CPUCapacityMismatch; SREGym resource request too large | P2 |
| M029 | CPU limit too low (throttling) | k8s workload | latency, slow probes; no K8s event | NOT detected | same manifest | yes | yes (needs metrics to see it) | k8s.af CPU throttling stories (search summary); COB CPU overload | P3 |
| M030 | Requests too small, node overcommitted, pod evicted or starved | scheduling | evictions or latency under pressure | NOT detected | same manifest | yes | no (single node, no pressure) | SREGym resource request too small | P3 |
| M031 | request greater than limit or bad quantity unit | k8s workload | Argo sync error, object never created | NOT detected | same manifest | yes | yes | ConfDef; F14 design note | P2 |
| M032 | Namespace ResourceQuota exceeded | control plane | ReplicaSet FailedCreate 'exceeded quota', no pod | NOT detected | other manifest (quota) or escalate | yes | yes | COB namespace quota; SREGym namespace memory limit | P2 |
| M033 | LimitRange default or max violated | control plane | pod rejected at admission | NOT detected | other manifest (LimitRange) | yes | yes | COB admission control | P3 |
| M034 | Memory leak in application | app | slow growth then OOMKilled | OOMKilled | app code | escalate | needs-X (app change) | COB memory leak; SREGym | P3 |
| M035 | Busy loop or artificial delay in application | app | CPU saturation, latency | NOT detected | app code | escalate | needs-X (app change) | COB application code defect | P3 |
| M036 | GC pressure or manual GC degrades capacity | app | latency, throughput drop | NOT detected | app config or code | escalate | needs-X (app change, metrics) | SREGym ad service manual gc, GOGC, gc capacity degradation | P3 |
| M037 | Replicas set to zero | k8s workload | no pods; dependents fail; Service has no endpoints | NOT detected | same manifest | yes | yes (F20: seen via workflow) | COB zero replicas; SREGym scale pod zero; ours F20 | P1 |
| M038 | Rolling update misconfigured (maxSurge 0 / maxUnavailable 100%) | k8s workload | brief or total outage on rollout | NOT detected | same manifest | yes | yes | SREGym rolling update misconfigured, operator wrong update strategy | P2 |
| M039 | Rollout stuck, ProgressDeadlineExceeded | k8s workload | Deployment condition false; old pods remain | NOT detected | depends on cause | yes | yes | Argo practice; F13/F14 side effect | P2 |
| M040 | Deployment selector or label mismatch (immutable selector change) | k8s workload | Argo sync error 'field is immutable' | NOT detected | same manifest | yes | yes | Argo practice (search summary) | P2 |
| M041 | Init container fails | k8s workload | pod Init:CrashLoopBackOff | NOT detected | same manifest | yes | yes (code reading: initContainerStatuses not inspected; not live-tested) | COB startup; detectors.py | P2 |
| M042 | Sidecar fails or sidecar and app bind the same port | k8s workload | second container 'address already in use' | CrashLoopBackOff | same manifest | yes | yes (not implemented) | SREGym sidecar port conflict | P2 |
| M043 | Missing ConfigMap used as a volume | k8s workload | ContainerCreating, FailedMount events | NOT detected | same or other manifest | yes | yes (known gap) | COB volume mount; our taxonomy 'rejected' | P2 |
| M044 | Job or CronJob pod fails past backoffLimit | k8s workload | Pod Error, Job Failed; no CrashLoopBackOff | NOT detected | same manifest | yes | yes | Argo/K8s practice | P2 |
| M045 | Service targetPort does not match the container port | k8s network | no traffic; probes OK; dependent step fails | Workflow failed | same manifest | yes | yes (F17) | COB Service port mapping; SREGym k8s target port misconfig; ours F17 | P1 |
| M046 | Service selector matches no pod (wrong label) | k8s network | Service has no endpoints; pods Running | NOT detected | same manifest | yes | yes | COB Service selector; SREGym wrong service selector | P2 |
| M047 | Service renamed or missing, dependents cannot resolve | k8s network | dependent nginx 'host not found in upstream' after restart | CrashLoopBackOff | other manifest (Service file) | yes | yes (F10) | SREGym missing service; COB ServiceDNSResolutionFailure; ours F10 | P1 |
| M048 | NetworkPolicy blocks required traffic (api->redis, workflow->api) | k8s network | timeouts; dependent step fails | NOT detected | other manifest (networkpolicy.yaml) | yes | yes (kindnet enforces; surfaces as a failed workflow smoke test) | SREGym network policy block; NetArena; COB | P2 |
| M049 | Default-deny egress blocks DNS | k8s network | all name resolution fails | NOT detected | other manifest | yes | yes | NetArena (search summary) | P3 |
| M050 | dnsPolicy/dnsConfig wrong (e.g. None with bad nameserver) | k8s network | app resolution fails; nginx exits at start | CrashLoopBackOff | same manifest | yes | yes (not implemented) | SREGym wrong dns policy | P2 |
| M051 | CoreDNS config stale or CoreDNS down | control plane | cluster-wide resolution failure | NOT detected | cluster-only (kube-system ConfigMap) | escalate | yes (cluster action) | SREGym stale coredns config; COB DNS failure; k8s.af | P2 |
| M052 | DNS ndots/search path latency and conntrack races | k8s network | intermittent slow or failed lookups | NOT detected | same manifest (dnsConfig) or node | escalate | no | k8s.af DNS/conntrack stories (search summary) | P3 |
| M053 | Ingress or gateway misroute | k8s network | 404/502 at the edge, pods healthy | NOT detected | other manifest | yes | no (no ingress controller) | SREGym ingress misroute; COB gateway misroute | P3 |
| M054 | nodeSelector matches no node | scheduling | new pod Pending, 'didn't match node selector' | FailedScheduling | same manifest | yes | yes (F25) | COB node selector mismatch; ours F25 | P1 |
| M055 | Node tainted, pod has no toleration | scheduling | Pending, 'untolerated taint' | FailedScheduling | cluster (remove taint) or manifest (add toleration) | escalate | yes (F7) | COB taint/toleration; SREGym taint no toleration; ours F7 | P1 |
| M056 | Required pod anti-affinity cannot be satisfied (single node) | scheduling | second replica Pending, 'didn't match pod anti-affinity rules' | FailedScheduling | same manifest | yes | yes (F24) | SREGym pod anti affinity deadlock; ours F24 | P1 |
| M057 | Invalid affinity or toleration syntax / non-matching affinity | scheduling | Pending or Argo sync error | FailedScheduling | same manifest | yes | yes | SREGym operator invalid affinity toleration; COB | P2 |
| M058 | Pod pinned to a non-existent node (spec.nodeName) | scheduling | Pending forever, scheduler never involved, no FailedScheduling | NOT detected | same manifest | yes | yes | SREGym assign to non existent node | P2 |
| M059 | PVC references a StorageClass that does not exist | k8s storage | Pending, unbound PVC | FailedScheduling | other manifest (PVC) | yes | yes (F15) | COB PVCStorageClassMismatch; SREGym operator non existent storage; ours F15 | P1 |
| M060 | PVC access mode not supported by the provisioner (RWX on local-path) | k8s storage | PVC never binds, pod Pending; only PVC ProvisioningFailed events, no FailedScheduling (live: 2.5 min) | NOT detected | other manifest (PVC) | yes | yes (tried, dropped: not detectable) | COB PVC access mode; ours (rejected candidate) | P2 |
| M061 | Pod claimName does not match any PVC | k8s storage | Pending, 'persistentvolumeclaim not found' | FailedScheduling | same manifest | yes | yes | SREGym pvc claim mismatch | P2 |
| M062 | PV node affinity conflicts with pod placement | k8s storage | Pending, 'volume node affinity conflict' | FailedScheduling | cluster/storage | escalate | no (single node) | SREGym persistent volume affinity violation | P3 |
| M063 | Same RWO PVC mounted by several pods on different nodes | k8s storage | Multi-Attach error, pod stuck ContainerCreating | NOT detected | same manifest | yes | no (single node) | SREGym duplicate pvc mounts | P3 |
| M064 | PVC capacity too small or volume full | k8s storage | write errors, pod evicted or crashing | NOT detected | other manifest (PVC) | yes | needs-X (writing app; local-path does not enforce size) | COB PVC capacity; Gunawi | P3 |
| M065 | emptyDir sizeLimit exceeded or node disk pressure | node-OS-HW | pod evicted (phase Failed, reason Evicted) | NOT detected | same manifest or node | yes | needs-X | k8s.af eviction stories (search summary) | P3 |
| M066 | etcd data corruption or bit flip in stored objects | control plane | Mutiny: cluster-wide stall, under/over-provisioning, bad networking | NOT detected | cluster-only | no | no | Mutiny (DSN'24) | P3 |
| M067 | API server or etcd unavailable | control plane | no changes possible; controllers stall | NOT detected | cluster-only | no | no (would also stop the operator) | Mutiny; Gunawi | P3 |
| M068 | Scheduler down | control plane | new pods Pending with no events | NOT detected | cluster-only | no | yes (kind static pod; risky) | COB infrastructure; Mutiny | P3 |
| M069 | kubelet crash / node NotReady | node-OS-HW | pods Unknown, NodeNotReady | NOT detected | cluster / hardware | no | yes (stop kubelet in the kind node container) | SREGym kubelet crash; COB kubelet unavailable | P2 |
| M070 | kube-proxy or CNI failure | node-OS-HW | Service IPs unreachable, pod network broken | NOT detected | cluster-only | no | yes (delete kube-proxy daemonset pod; recovers) | COB kube-proxy unavailable | P3 |
| M071 | containerd runtime failure | node-OS-HW | pods stuck ContainerCreating / sandbox errors | NOT detected | cluster-only | no | yes (in kind node; risky) | COB containerd unavailable | P3 |
| M072 | Admission webhook unavailable with failurePolicy Fail | control plane | all creates/updates in scope rejected | NOT detected | other manifest (webhook config) or escalate | yes | no (no webhook installed) | k8s.af (search summary); Argo practice | P3 |
| M073 | RBAC misconfiguration (ServiceAccount cannot do what the app needs) | control plane | app/controller logs 403 forbidden | NOT detected | other manifest (Role/RoleBinding) | yes | needs-X (workload that calls the API) | SREGym rbac misconfiguration; COB missing ServiceAccount | P2 |
| M074 | Referenced ServiceAccount missing | control plane | ReplicaSet FailedCreate, no pod | NOT detected | other manifest | yes | yes | COB missing ServiceAccount | P2 |
| M075 | Operator/controller upgrade changes CRD behaviour (version skew) | operator | CRs reconcile wrongly, no pod failure | NOT detected | other manifest or escalate | escalate | no | Gunawi UPGRADE; Mutiny | P3 |
| M076 | Node CPU or memory stress (noisy neighbour) | node-OS-HW | latency, evictions | NOT detected | hardware / node | no | needs-X (stress pod) | SREGym noise node resource stress; COB CPU overload | P3 |
| M077 | Disk latent sector error / disk corruption | node-OS-HW | I/O errors in pods using that disk | NOT detected | hardware | no | no | SREGym latent sector error, disk sector corruption | P3 |
| M078 | Silent data corruption | node-OS-HW | wrong results, no error | NOT detected | hardware / app | no | no | SREGym silent data corruption | P3 |
| M079 | Syscall failure injected with eBPF | node-OS-HW | random app errors | NOT detected | node | no | no | SREGym eBPF syscall failure | P3 |
| M080 | conntrack table full / port exhaustion | node-OS-HW | dropped connections | NOT detected | node sysctl | no | no | k8s.af conntrack stories (search summary) | P3 |
| M081 | Invalid workflow parameter value | CI-CD pipeline | check step fails, 'unknown mode' | Workflow failed | same manifest (WorkflowTemplate) | yes | yes (F6) | ours F6 | P1 |
| M082 | Workflow step uses wrong image tag or command | CI-CD pipeline | step Error, ImagePull in workflow pod | Workflow failed | same manifest | yes | yes | ours (rejected as duplicate of F6) | P2 |
| M083 | Smoke test target unreachable (Service port, scale zero, rename) | CI-CD pipeline | smoke-test step times out or refuses | Workflow failed | other manifest (Service/Deployment) | yes | yes (F17, F20; baseline note in doc) | ours F17/F20 | P1 |
| M084 | WorkflowTemplate reference missing or renamed | CI-CD pipeline | Workflow Error at start | Workflow failed | same manifest | yes | yes | Argo practice | P2 |
| M085 | Workflow ServiceAccount lacks permission for pods/patch | CI-CD pipeline | executor Error | Workflow failed | other manifest (RBAC) | yes | yes | Argo Workflows practice; SREGym rbac | P2 |
| M086 | Workflow timeout / activeDeadline too short | CI-CD pipeline | Workflow Failed 'Deadline exceeded' | Workflow failed | same manifest | yes | yes | Argo practice (seen live: F20 run hit 'Deadline exceeded' log) | P3 |
| M087 | Argo CD sync fails on an invalid manifest (schema, immutable field) | CI-CD pipeline | Application OutOfSync/SyncFailed; pods unchanged | NOT detected | same manifest | yes | yes | Argo practice (search summary); ConfDef | P2 |
| M088 | Argo CD cannot reach repo or credentials expired (ComparisonError) | CI-CD pipeline | Application Unknown; nothing deploys | NOT detected | cluster / Argo | escalate | yes (cluster action) | Argo practice (search summary) | P2 |
| M089 | Persistent OutOfSync: HPA/webhook mutates a field Argo owns (selfHeal fights) | CI-CD pipeline | Application flaps OutOfSync | NOT detected | Argo ignoreDifferences | yes | no | Argo practice (search summary) | P3 |
| M090 | Manual kubectl edit reverted by selfHeal or left as drift | CI-CD pipeline | config differs from git; behaviour flips on resync | NOT detected | git history / cluster | yes | yes | SREGym configmap drift; Argo practice | P2 |
| M091 | PreSync hook Job stuck or failing blocks the sync | CI-CD pipeline | sync Running forever | NOT detected | other manifest (hook) | yes | yes | Argo practice (search summary) | P3 |
| M092 | Resource ordering: CRD or namespace missing at apply (sync waves) | CI-CD pipeline | SyncFailed 'no matches for kind' | NOT detected | same manifest | yes | yes | Argo practice | P3 |
| M093 | Bad merge: wrong overlay/path, file moved out of the synced directory | CI-CD pipeline | resources pruned, workload disappears | NOT detected | git history | yes | yes | Argo practice (prune) | P2 |
| M094 | Buggy or misconfigured operator CR (wrong image, replicas, strategy) | operator | managed workload crashes or is wrong | CrashLoopBackOff | CR manifest | yes | no (no custom operator in demo) | SREGym operator wrong image / overload replicas / wrong update strategy / security context | P3 |
| M095 | Operator reconcile loop fights the repair (reverts the fix) | operator | fix is undone | NOT detected | operator spec | escalate | no | Gunawi UPGRADE/BUGS; thesis risk | P3 |
| M096 | SQL column name mismatch / bad query | data | 5xx for some endpoints | NOT detected | app code | escalate | no (no SQL) | SREGym sql column name mismatch, nested sql select error | P3 |
| M097 | Database read-only, connection exhaustion | data | write errors, timeouts | NOT detected | backend config | escalate | no | COB MySQL read-only, connection exhaustion | P3 |
| M098 | Cache memory pressure / eviction misconfig (valkey/redis) | data | hit rate drop, latency | NOT detected | same manifest | yes | yes (redis maxmemory; needs client) | SREGym valkey memory disruption | P3 |
| M099 | Load flood or retry storm tips a service into metastable failure | load | latency collapse persists after the trigger ends | NOT detected | capacity/retry config; not a single manifest | escalate | needs-X (load gen, metrics) | Metastable failures (HotOS'21); SREGym loadgenerator flood, capacity decrease rpc retry storm | P3 |
| M100 | Workload imbalance across replicas | load | one pod hot, others idle | NOT detected | app/LB config | yes | no (single node, no metrics) | SREGym workload imbalance | P3 |
| M101 | Gray failure: component looks healthy to the probe but not to clients | app | probes pass, users see errors | NOT detected | app/probe design | escalate | needs-X (app change) | Gray Failure (HotOS'17) | P3 |
| M102 | Upgrade or release introduces incompatibility between components | CI-CD pipeline | mixed-version errors during rollout | NOT detected | git history | yes | needs-X | Gunawi UPGRADE; Liu19 data-format bugs; SREGym update incompatible correlated | P2 |
| M103 | Data-format or constant-value bug in a release | app | parse errors after deploy | NOT detected | app code | escalate | needs-X (app change) | Liu19 (data format 21%, constant-value bugs) | P3 |
| M104 | CAS unreachable or wrong CAS address in the SCONE session config | confidential (planned) | enclave fails attestation at start; container exits | CrashLoopBackOff | same manifest (SCONE_CAS_ADDR) or CAS | yes | needs SCONE (planned) | ours (thesis contribution) | P2 |
| M105 | MRENCLAVE / policy hash mismatch after an image rebuild | confidential (planned) | attestation rejected by CAS; app exits | CrashLoopBackOff | CAS session policy (not the workload manifest) | escalate | needs SCONE (planned) | ours | P2 |
| M106 | CAS session or secret missing, expired or not yet uploaded | confidential (planned) | app cannot obtain secrets; exits | CrashLoopBackOff | CAS (cluster-external) | escalate | needs SCONE (planned) | ours | P2 |
| M107 | SCONE_MODE hw requested on a node without SGX (or sim in production) | confidential (planned) | enclave fails to start / policy rejects sim mode | CrashLoopBackOff | same manifest or node | yes | needs SCONE (planned) | ours | P2 |
| M108 | SGX device-plugin resource missing (sgx.intel.com/enclave unavailable) | confidential (planned) | pod Pending, 'Insufficient sgx.intel.com/enclave' | FailedScheduling | node / device plugin; escalate | escalate | needs SCONE (planned); the Pending mechanics are testable with a fake extended resource | ours | P2 |
| M109 | Enclave heap (SCONE_HEAP) larger than EPC or container memory limit | confidential (planned) | enclave OOM or OOMKilled | OOMKilled | same manifest | yes | needs SCONE (planned) | ours | P2 |
| M110 | Local attestation service (LAS) down | confidential (planned) | attestation timeouts across pods | CrashLoopBackOff | cluster-only | no | needs SCONE (planned) | ours | P3 |
| M111 | Encrypted file-system key or tag mismatch | confidential (planned) | app cannot open protected files | CrashLoopBackOff | CAS / data | escalate | needs SCONE (planned) | ours | P3 |
| M112 | Attestation succeeds but the CAS policy is too permissive or stale (security-relevant misconfiguration, not a crash) | confidential (planned) | none visible | NOT detected | CAS | escalate | needs SCONE (planned) | ours | P3 |


## Coverage

Rows: 112. Priority P1 24, P2 46, P3 42. Operator detects 51 of 112 rows (61 NOT detected). Rows backed by at least one F-fault: 24.

| layer | rows | detected by the operator | our F-ids | rows with an F-id |
|---|---|---|---|---|
| config | 10 | 9 | F3, F4, F8, F11, F12, F18, F21, F22 | 7 |
| k8s workload | 25 | 14 | F1, F2, F5, F9, F13, F14, F16, F19, F20, F23, F26, F27 | 8 |
| control plane | 10 | 0 | none | 0 |
| scheduling | 8 | 6 | F7, F14, F24, F25 | 4 |
| app | 5 | 1 | none | 0 |
| k8s network | 8 | 3 | F10, F17 | 2 |
| k8s storage | 6 | 3 | F15 | 1 |
| node-OS-HW | 9 | 0 | none | 0 |
| operator | 3 | 1 | none | 0 |
| CI-CD pipeline | 14 | 6 | F6, F17, F20 | 2 |
| data | 3 | 0 | none | 0 |
| load | 2 | 0 | none | 0 |
| confidential (planned) | 9 | 8 | none | 0 |
| **all** | **112** | **51** | F1-F27 | **24** |

What F1-F27 cover, by layer:

- **config**: missing key (F4, F8), envFrom shadowing (F21), missing ConfigMap (F3), renamed ConfigMap (F12), subPath key (F11), missing Secret key (F18), bad third-party flag (F22).
- **k8s workload**: OOM (F1, F9, F26), wrong command (F5), securityContext (F16), liveness port (F13) and path (F19), bad image (F2, F27), private image without pull secret (F23), scale to zero (F20).
- **k8s network**: Service rename with a restarting dependent (F10), wrong targetPort seen through a failed smoke test (F17).
- **scheduling / storage**: untolerated taint (F7), oversized request (F14), nodeSelector (F25), PVC with missing StorageClass (F15), required anti-affinity on one node (F24).
- **CI-CD pipeline**: bad workflow parameter (F6), smoke test against a broken Service or a scaled-down API (F17, F20).
- **compound / noise / safety**: two faults in one commit (F26), unrelated pod restarts during a real fault (F27), prompt injection in repo content (F8).
- **not covered at all**: control plane, node-OS-HW, operator, data, load and confidential. These are the rows the demo cannot reproduce, or where the operator has nothing to detect (next section).

By Cloud-OpsBench category: Scheduling (F7, F14, F15, F24, F25), Startup (F2, F3, F12, F16, F18, F23, F27), Runtime (F1, F4, F5, F8, F9, F11, F13, F19, F20, F21, F22, F26), Service Routing (F10, F17). Admission Control, Performance, Infrastructure and Application Code Defect have no fault. Our own dimensions that COB lacks: CI/CD pipeline failures (F6, F17, F20), safety (F8), noise and compounds (F26, F27), fix-location variety (same manifest, other manifest, git history, cluster-only).

## Gaps we cannot reach and why

1. **Detector limits (the largest gap).** The operator opens an incident only for the six symptoms in the header. Whole groups of rows end in `NOT detected` even though they are fixable in git and demo-feasible:
   - readiness-only failures (pod Running, never Ready; Service without endpoints), wrong Service selector, NetworkPolicy blocks, DNS policy faults: the pod does not fail, the dependent does. They only surface if a workflow step or a restarting dependent happens to fail (which is how F17, F20 and F10 are detectable).
   - **init container failures**: the detector reads `containerStatuses` only, so `Init:CrashLoopBackOff` is invisible (read from the code; the status is standard Kubernetes behaviour, not live-tested here).
   - **`CreateContainerError` / `RunContainerError`** (binary not found, runtime errors) are not in `POD_REASONS`.
   - **missing ConfigMap used as a volume** leaves the pod in `ContainerCreating` with `FailedMount` events.
   - **PVC provisioning failures under `WaitForFirstConsumer`** (the kind default): an unsupported access mode (RWX on local-path) leaves the pod Pending with only `ProvisioningFailed` events on the PVC, and no `FailedScheduling` event (observed live over 2.5 min; the one `FailedScheduling` that appeared came from the pod being deleted at revert). F15 (unknown StorageClass) is detectable only because the class is missing at scheduling time.
   - **admission rejections** (ResourceQuota, LimitRange, missing ServiceAccount, PSA) leave a ReplicaSet with `FailedCreate` and no pod to inspect.
   - **`spec.nodeName` pinned to a missing node** bypasses the scheduler, so no `FailedScheduling` event exists.
   - **Argo CD sync errors** (invalid manifest, immutable field, repo credentials, hooks) leave an Application `OutOfSync`/`SyncFailed`/`Unknown` while the running pods are unchanged. No Application-level detector exists.
   - **Evicted pods, Jobs past `backoffLimit`, stalled rollouts (`ProgressDeadlineExceeded`)** are not waiting reasons.
   Extending the detector to Application health, Deployment conditions and init statuses would turn about a dozen P2 rows into reachable faults; that is the cheapest way to widen coverage.
2. **Single node.** Anti-affinity across nodes, PV node-affinity conflicts, multi-attach errors, PodDisruptionBudgets, drains, workload imbalance and node-to-node packet loss need at least two nodes. Required pod anti-affinity can still be shown on one node (second replica Pending).
3. **No metrics stack.** CPU throttling, latency, gray failures, retry storms and metastable failures are visible only in metrics. Without Prometheus (and without a load generator) the loop cannot see them, and a repair cannot be verified except by "pods Ready".
4. **No application-code change allowed in the demo.** Memory leaks, busy loops, SQL bugs, GC pressure, data-format bugs and gray failures need an app that misbehaves on demand. The app code lives in ConfigMaps, so a "code" fault is possible technically (F8 edits it), but it would be an app-code repair, which is outside the GitOps-config scope.
5. **Cluster-only and hardware faults.** etcd corruption, API-server loss, expired certificates, kubelet/containerd failure, disk errors and silent data corruption cannot be fixed by a git change; the correct behaviour is escalation. Some can be injected in kind (stop the kubelet, edit the CoreDNS ConfigMap), but the loop would have nothing to propose, and stopping the kubelet or the API server would also stop the operator itself.
6. **Stateful backends.** Auth faults, read-only databases, connection exhaustion and queue problems need a real backend (MongoDB, MySQL, Kafka). The demo has only redis and no client of it.
7. **Confidential computing (SCONE/CAS/attestation).** No SGX hardware or SCONE runtime on the kind cluster; every row is planned. The scheduling mechanics of a missing `sgx.intel.com/enclave` resource can be mimicked with a fake extended resource and give a `FailedScheduling` that git cannot fix.
8. **Fixes outside git.** Registry credentials, CAS sessions, secret values and node taints are not repository content, so the loop can only escalate (F7, F23 are the two escalation faults).

## Noise and compound faults we could add

Implemented: F26 (OOM plus wrong liveness path in one commit; fixing the first reveals the second) and F27 (bad worker image while redis and frontend pods are deleted). Ideas, following SREGym's noise injectors and failure modes:

- **Noise**: pause or restart unrelated pods repeatedly (a loop of `kubectl delete pod` on healthy workloads); a harmless Warning event stream; a stuck completed Job; a second namespace with a known-flaky pod (F9 style) while another fault is being repaired. Goal: check that incidents are not opened for self-healing restarts and that the evidence bundle is not polluted.
- **Latency/packet loss noise**: needs `tc` or a chaos tool inside the kind node; no metric would show it.
- **Compounds across files**: a fault in `configmaps.yaml` plus a different one in `api.yaml` (needs a multi-file injector; the current injector edits one file per fault). SREGym's "correlated" faults (one bad image tag shared by several services) fit once a second workload uses the same tag.
- **Masked faults**: F26 generalises to "fault B only appears after fault A is fixed"; more pairs: wrong Service targetPort behind a wrong liveness path, quota exceeded behind an invalid image tag.
- **Retry/metastable**: a workflow step that retries a failing smoke test with a short interval (retry storm) once a load generator exists.
- **Time-delayed faults**: a CronJob or a rolling restart that makes the fault show only after a later restart (F10-F12 already need a restart; `restart_target` in the catalog records which deployment).
- **Fix-fights**: an operator-style controller or a second Argo Application that reverts the repair (row on operator reconcile fights), to test escalation.

## New faults F17-F27 (this change)

Each fault was injected once, one at a time, through `python -m repair_faults inject Fn`, observed on `kind-repair` (hard-refresh of the Argo CD app after the commit), and reverted. Baseline for all runs: demo repo `main` with Service `api-svc` (see the baseline note below). The repair operator was not running; "detector class" is what `detectors.py` would classify from the observed state.

| id | design (file) | observed symptom | detector class | outcome expected |
|---|---|---|---|---|
| F17 | api Service `targetPort` 8000 -> 8080 (`api.yaml`); injector waits for Argo sync, then submits a release-pipeline run | api pod Running/Ready; smoke-test step `curl: (28) Connection timed out after 10002 milliseconds` (NetworkPolicy also only allows 8000), Workflow Failed after about 30s | Workflow failed (STAGE_FAILURE) | Healed |
| F18 | worker env `WORKER_TOKEN` from `secretKeyRef worker-credentials/WORKER_TOKEN`; Secret added with only `WORKER_ID` (`worker.yaml`) | new worker pod `CreateContainerConfigError`: `couldn't find key WORKER_TOKEN in Secret demo/worker-credentials`; old pod keeps running | CreateContainerConfigError | Healed |
| F19 | api livenessProbe path `/healthz` -> `/livez` (`api.yaml`) | `Liveness probe failed: HTTP probe failed with statuscode: 404`; first restart after about 40s; `CrashLoopBackOff` only after 5 restarts (about 6.5 min), exit code 137 | CrashLoopBackOff (slow) | Healed |
| F20 | api `replicas` 1 -> 0 (`api.yaml`); run submitted after sync plus 40s | no api pod; smoke-test `curl: (28)` timeout, Workflow Failed. Without the 40s wait the run passed, because the terminating pod still serves traffic during its 30s grace period | Workflow failed (STAGE_FAILURE) | Healed |
| F21 | explicit `env: DB_URL: ""` on the api container shadows `envFrom` (`api.yaml`) | new api pod `CrashLoopBackOff` within 25s (app exits "DB_URL not set", code path); `api-config` still contains DB_URL | CrashLoopBackOff | Healed |
| F22 | redis `--maxmemory-policy allkeys-lru` -> `allkeys-lfu-max` (`redis.yaml`) | new redis pod `CrashLoopBackOff` (2 restarts in 23s), readiness refused, old pod keeps running. Container log text not captured | CrashLoopBackOff | Healed |
| F23 | api-b image -> `ghcr.io/akashdeepsinghjassal/api-b-private:1.0.0` plus `imagePullSecrets: regcred` (`deploy/demo-b/api-b.yaml`) | `ImagePullBackOff`/`ErrImagePull`, events `FailedToRetrieveImagePullSecret (regcred)` and `failed to resolve` the image | ImagePull | Escalated (credential is not in git) |
| F24 | worker `replicas` 2 with required podAntiAffinity on hostname (`worker.yaml`) | extra worker pod Pending, `FailedScheduling: 0/1 nodes are available: 1 node(s) didn't match pod anti-affinity rules` (event at about 1.5 min) | FailedScheduling | Healed |
| F25 | frontend `nodeSelector: disktype: ssd` (`frontend.yaml`) | new frontend pod Pending, `FailedScheduling: 1 node(s) didn't match Pod's node affinity/selector`; old pod keeps serving | FailedScheduling | Healed |
| F26 | compound in `api.yaml`: memory limit 32Mi plus liveness path `/livez` | new api pod `CrashLoopBackOff` within 25s (OOM kill, exit 137, hides the probe fault) | CrashLoopBackOff (OOMKilled in lastState) | Healed (needs two fixes) |
| F27 | worker image `python:3.12-alpine3.99`; injector also deletes the redis and frontend pods (`worker.yaml`) | worker `ImagePullBackOff`, `failed to pull ... NotFound`; redis and frontend pods restart with transient readiness warnings, recover alone | ImagePull | Healed |

`restart_target` is set only for F10, F11 (frontend) and F12 (api-b), where the running pod is unaffected until restart. All new entries carry `evidence_milestones`, as do F1-F16.

Injector additions (all optional catalog keys, covered by tests): `submit_workflow` + `argo_app` (+ `submit_delay_seconds`) wait for Argo CD to sync the commit and then submit a `release-pipeline` run, so faults whose symptom is a failing workflow step (F17, F20) can be injected in one command; `post_inject_commands`/`post_revert_commands` run kubectl after the commit (F27's noise); `restart_target` and `evidence_milestones` are validated.

Rejected or dropped candidates:

- **RWX PVC on the local-path class** (tried as F24): pod Pending 2.5 min with only `WaitForFirstConsumer`/`ProvisioningFailed` events on the PVC; no `FailedScheduling`, so the detector never fires. Replaced by required anti-affinity.
- **Init container failure**: `initContainerStatuses` is not read by the detector (code reading, not live-tested).
- **Readiness probe wrong path/port**: pod Running but never Ready; no waiting reason; rollout stalls silently (not live-tested; same detector argument).
- **Resource quota / LimitRange / missing ServiceAccount**: ReplicaSet `FailedCreate`, no pod to detect.
- **Command binary not found, `spec.nodeName` on a missing node, missing ConfigMap volume**: `CreateContainerError`, no scheduler event, `FailedMount`; all outside the detector set.
- **Resource request too small causing OOM in another workload**: a single-node kind cluster without memory pressure never evicts or OOM-kills a neighbour; F1-style limits already cover OOM.
- **Scale-to-zero with a crashing dependent**: nothing in the demo crashes when api disappears (nginx resolves the Service name, which still exists); the only dependent that fails is the workflow smoke test, so F20 is detected through F17's mechanism.
- **Image pull secret missing with a public image**: the pull still succeeds (only a warning event); F23 uses a private path instead.
- **Fully separate pod and Service targetPort crash for the frontend**: nginx only resolves the Service name, not the port, at start.

Baseline notes found during verification (demo repo, not fixed here):

1. After the merged F10 repair the demo repo uses Service `api-svc` and the frontend `proxy_pass http://api-svc:8000/`; the F10 fault was rewritten to rename `api-svc` -> `api` (same effect: nginx cannot resolve its upstream on restart), and the F10 fixtures in `tests/faults/fixtures` were refreshed from the repo.
2. `workflows/release-pipeline.yaml` still curls `http://api.demo.svc:8000/healthz`, which no longer resolves (`curl: (6)`), so the baseline `release-pipeline` run **fails** even when everything is healthy. To verify F17 and F20 I temporarily created a cluster-only `ExternalName` Service `api` -> `api-svc` (deleted afterwards). F17 and F20 need the smoke-test URL changed to `api-svc.demo.svc` in the demo repo (or an alias Service added) to be meaningful, and the demo README still says `api`.
3. The F20 run showed a workflow controller delay: the Workflow stayed `Running` for about 5 minutes after its smoke-test pod had failed before turning `Failed` ("Deadline exceeded" in the controller log).


## Sources

Read in full by the thesis author, and used from the repo notes: Cloud-OpsBench (arXiv 2603.00468, `research/cloud-opsbench-overview.md`) and SREGym (arXiv 2605.07161, `research/sregym-overview.md`). The problem list quoted in issue #27 is taken from the SREGym reading, not from this search.

**Seen only as search summaries (not opened in full; figures below come from the summaries and should be checked against the papers before citing):**

- Gunawi et al., "Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages", SoCC 2016: 597 outages in 32 services, 2009-2015; 13 root causes; upgrade, network, bugs, config and load named most common by a blog review (355 outages had unknown root cause). Paper PDF: https://www.ece.iastate.edu/~mai/docs/papers/2016_SoCC_COS.pdf ; review: https://muratbuffalo.blogspot.com/2016/11/why-does-cloud-stop-computing-lessons.html
- Liu, Lu, Musuvathi, Nath, "What Bugs Cause Production Cloud Incidents?", HotOS 2019: Azure high-severity incidents; software bugs about 40%; data-format, fault-handling, timing and constant-value bugs largest. Summary: https://blog.acolyer.org/2019/06/21/what-bugs-cause-cloud-production-incidents/
- Zhang, Paul, d'Amorim, Rahman, "Configuration Defects in Kubernetes", arXiv 2512.05062: 719 defects from 185 repositories, 15 defect categories, 52 defects of "configuration inexecutability" (settings never applied or overridden). https://arxiv.org/pdf/2512.05062
- Barletta, Cinque, Di Martino, Kalbarczyk, Iyer, "Mutiny! How does Kubernetes fail, and what can we do about it?", DSN 2024, arXiv 2404.11169: about 9,000 injections into etcd data (bit-flips, data-type corruption, message drops); about 3% system-wide failures; errors in object-dependency fields 51% of those. https://arxiv.org/abs/2404.11169
- Huang et al., "Gray Failure: The Achilles' Heel of Cloud-Scale Systems", HotOS 2017: differential observability between the detector and the application. https://www.microsoft.com/en-us/research/publication/gray-failure-achilles-heel-cloud-scale-systems/
- Bronson et al., "Metastable Failures in Distributed Systems", HotOS 2021: trigger versus sustaining effect, retries as a typical sustaining loop. https://brooker.co.za/blog/2021/05/24/metastable/ (secondary)
- Kubernetes Failure Stories (k8s.af): CPU limit throttling, CoreDNS/ndots/conntrack, node pressure and eviction entries. https://k8s.af/ (listing only; individual postmortems not opened)
- Argo CD troubleshooting guides (practitioner blogs): persistent OutOfSync from mutated fields, prune behaviour, stuck hooks, ComparisonError. https://oneuptime.com/blog/post/2026-02-02-argocd-debugging/markdown (third-party, not official docs)
- Earlier notes in the repo: `plan-v3/failure-taxonomy.md` (our F1-F16 taxonomy; its sources were also search summaries), AIOpsLab, NetArena.

The `fixable in git`, `fix lives` and `demo-feasible` columns are my own judgement, informed by the live runs of F17-F27 (this change) and the earlier F11-F16 runs; the rows marked "not live-tested" are reasoning from the detector code or Kubernetes behaviour.
