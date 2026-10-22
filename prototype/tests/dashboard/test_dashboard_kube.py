from repair_dashboard.kube import (
    parse_argocd_app,
    parse_incident,
    parse_incident_detail,
    parse_pod,
    parse_services,
    parse_workflow,
    service_status,
)

LABELS = {"app": "api"}


def deployment(ready=1, replicas=1):
    return {
        "metadata": {"name": "api", "namespace": "demo", "labels": {"app.kubernetes.io/version": "1.2.0"}},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": LABELS},
            "template": {"metadata": {"labels": LABELS}, "spec": {"containers": [{"name": "api", "image": "api:1.2.0"}]}},
        },
        "status": {"readyReplicas": ready},
    }


def pod(restarts=0, last=None, waiting=None, ready=True):
    cs = {"ready": ready, "restartCount": restarts, "state": {}, "lastState": {}}
    if last:
        cs["lastState"] = {"terminated": {"reason": last, "exitCode": 137}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting}}
    return {
        "metadata": {"name": "api-abc-12345", "namespace": "demo", "labels": LABELS},
        "status": {"phase": "Running", "containerStatuses": [cs]},
    }


SERVICE = {
    "metadata": {"name": "api", "namespace": "demo"},
    "spec": {"type": "NodePort", "selector": LABELS, "ports": [{"port": 80, "nodePort": 30080}]},
}


def test_parse_pod_oom_killed():
    p = parse_pod(pod(restarts=3, last="OOMKilled"))
    assert p["restarts"] == 3 and p["lastReason"] == "OOMKilled" and p["phase"] == "Running"


def test_parse_pod_crashloop_phase():
    assert parse_pod(pod(waiting="CrashLoopBackOff", ready=False))["phase"] == "CrashLoopBackOff"


def test_parse_services_joins_pods_and_service():
    [s] = parse_services([deployment()], [pod(restarts=0)], [SERVICE])
    assert (s["name"], s["namespace"], s["ready"], s["desired"]) == ("api", "demo", 1, 1)
    assert s["version"] == "1.2.0" and s["image"] == "api:1.2.0"
    assert s["service"] == {"name": "api", "type": "NodePort", "ports": [{"port": 80, "nodePort": 30080, "protocol": "TCP"}]}
    assert s["status"] == "green" and len(s["pods"]) == 1


def test_parse_services_ignores_other_namespace_pods():
    other = pod()
    other["metadata"]["namespace"] = "demo-b"
    [s] = parse_services([deployment()], [other], [])
    assert s["pods"] == [] and s["service"] is None


def test_service_status_levels():
    assert service_status(1, 1, [parse_pod(pod())]) == "green"
    assert service_status(1, 1, [parse_pod(pod(restarts=2, last="OOMKilled"))]) == "amber"
    assert service_status(0, 1, []) == "red"
    assert service_status(1, 1, [parse_pod(pod(waiting="ImagePullBackOff", ready=False))]) == "red"


def test_parse_argocd_app():
    a = parse_argocd_app(
        {
            "metadata": {"name": "demo-app"},
            "spec": {"destination": {"namespace": "demo"}},
            "status": {
                "sync": {"status": "Synced", "revision": "0123456789abcdef"},
                "health": {"status": "Healthy"},
                "operationState": {"finishedAt": "2026-01-01T00:00:00Z"},
            },
        }
    )
    assert a["sync"] == "Synced" and a["health"] == "Healthy" and a["shortRevision"] == "0123456"
    assert a["lastSync"] == "2026-01-01T00:00:00Z"


def test_parse_argocd_app_empty_status():
    a = parse_argocd_app({"metadata": {"name": "x"}})
    assert a["sync"] == "Unknown" and a["shortRevision"] == ""


def test_parse_workflow_failed_node():
    w = parse_workflow(
        {
            "metadata": {"name": "build-1", "namespace": "demo-b"},
            "spec": {"workflowTemplateRef": {"name": "ci"}},
            "status": {
                "phase": "Failed",
                "startedAt": "t1",
                "finishedAt": "t2",
                "nodes": {
                    "a": {"type": "Steps", "phase": "Failed"},
                    "b": {"type": "Pod", "phase": "Failed", "message": "Error (exit code 1)"},
                },
            },
        }
    )
    assert w["template"] == "ci" and w["phase"] == "Failed" and w["failedMessage"] == "Error (exit code 1)"


def test_parse_workflow_succeeded():
    w = parse_workflow({"metadata": {"name": "w"}, "status": {"phase": "Succeeded"}})
    assert w["failedMessage"] is None


RI = {
    "metadata": {"name": "ri-abc", "namespace": "demo", "creationTimestamp": "2026-01-01T00:00:00Z"},
    "spec": {"fingerprint": "f"},
    "status": {
        "phase": "AwaitingReview",
        "attempts": 1,
        "signature": "abc123",
        "classification": {"class": "OOM_KILLED", "confidence": 0.9, "source": "rules"},
        "proposal": {"tier": 1, "file": "deploy/demo/api.yaml", "edits": [], "rationale": "r", "needs_remeasure": False},
        "pullRequest": "https://github.com/x/y/pull/1",
        "timestamps": {"detected": "t0"},
    },
}


def test_parse_incident():
    i = parse_incident(RI)
    assert i["phase"] == "AwaitingReview" and i["class"] == "OOM_KILLED" and i["tier"] == 1
    assert i["pullRequest"].endswith("/pull/1") and i["attempts"] == 1 and i["timestamps"] == {"detected": "t0"}


def test_parse_incident_without_status():
    i = parse_incident({"metadata": {"name": "n", "namespace": "demo"}})
    assert i["phase"] == "Detected" and i["class"] is None and i["attempts"] == 0


def test_parse_incident_detail():
    d = parse_incident_detail(RI)
    assert d["signature"] == "abc123" and d["proposal"]["file"] == "deploy/demo/api.yaml" and d["reviewFeedback"] == []
