from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from repair_operator import detectors as d
from repair_operator.fingerprint import dedupe_fingerprint


def pod(statuses, labels=None, owner=("ReplicaSet", "api-7d9f8b6c5")):
    meta = {"name": "api-7d9f8b6c5-x2k9z", "namespace": "demo", "labels": labels or {}}
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1], "controller": True}]
    return {"metadata": meta, "spec": {"nodeName": "n1"}, "status": {"containerStatuses": statuses}}


def waiting(reason, name="api"):
    return {"name": name, "restartCount": 5, "state": {"waiting": {"reason": reason}}}


OOM = {
    "name": "api",
    "restartCount": 3,
    "state": {"running": {}},
    "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
}


def apps_api(deployment="api"):
    api = MagicMock()
    api.read_namespaced_replica_set.return_value = NS(
        metadata=NS(owner_references=[NS(kind="Deployment", name=deployment)])
    )
    return api


def test_reasons():
    for r, cls in [
        ("CrashLoopBackOff", "CRASH_LOOP"),
        ("ImagePullBackOff", "IMAGE_PULL"),
        ("ErrImagePull", "IMAGE_PULL"),
        ("CreateContainerConfigError", "CONFIG_ERROR"),
    ]:
        f = d.container_failure(waiting(r))
        assert f["reason"] == r and f["classHint"] == cls
    f = d.container_failure(OOM)
    assert (f["reason"], f["exitCode"], f["classHint"]) == ("OOMKilled", 137, "OOM_KILLED")
    assert d.container_failure({"name": "x", "state": {"running": {}}}) is None
    assert d.container_failure(waiting("ContainerCreating")) is None


def test_pod_spec_owner_and_fingerprint():
    (spec,) = d.pod_incident_specs("demo", pod([OOM]), apps_api())
    assert spec["source"]["owner"] == "Deployment/api"
    assert spec["source"]["container"] == "api"
    assert spec["fp"] == dedupe_fingerprint("demo", "Deployment/api", "api", "OOMKilled")


def test_replicas_share_fingerprint():
    other = pod([OOM])
    other["metadata"]["name"] = "api-7d9f8b6c5-zzzzz"
    a = d.pod_incident_specs("demo", pod([OOM]), apps_api())[0]["fp"]
    b = d.pod_incident_specs("demo", other, apps_api())[0]["fp"]
    assert a == b


def test_owner_fallbacks():
    assert d.resolve_owner(pod([], owner=("StatefulSet", "db"))) == "StatefulSet/db"
    assert d.resolve_owner(pod([], owner=None)) == "Pod/api-7d9f8b6c5-x2k9z"
    assert d.resolve_owner(pod([])) == "Deployment/api"  # no api: strip the hash suffix
    broken = MagicMock()
    broken.read_namespaced_replica_set.side_effect = RuntimeError("x")
    assert d.resolve_owner(pod([]), broken) == "Deployment/api"


def test_ignore_label():
    assert d.pod_incident_specs("demo", pod([OOM], labels={"repair.example.dev/ignore": "true"})) == []
    assert d.pod_incident_specs("demo", pod([{"name": "a", "state": {"running": {}}}])) == []


def wf(phase="Failed", labels=None, template="build"):
    return {
        "metadata": {"name": "build-xyz", "labels": labels or {}},
        "spec": {"workflowTemplateRef": {"name": template}},
        "status": {
            "phase": phase,
            "nodes": {
                "a": {"type": "Steps", "phase": "Failed", "displayName": "root"},
                "b": {"type": "Pod", "phase": "Succeeded", "displayName": "checkout"},
                "c": {"type": "Pod", "phase": "Failed", "displayName": "test"},
            },
        },
    }


def test_workflow_spec():
    s = d.workflow_incident_spec("demo", wf())
    assert s["source"]["node"] == "test" and s["source"]["template"] == "build"
    assert s["source"]["classHint"] == "STAGE_FAILURE"
    assert s["fp"] == dedupe_fingerprint("demo", "build", "test", "WorkflowFailed")
    assert d.workflow_incident_spec("demo", wf("Error")) is not None
    assert d.workflow_incident_spec("demo", wf("Succeeded")) is None
    assert d.workflow_incident_spec("demo", wf(labels={"repair.example.dev/ignore": "true"})) is None


def test_workflow_without_template_ref_uses_name():
    w = wf()
    w["spec"] = {}
    assert d.workflow_incident_spec("demo", w)["source"]["template"] == "build-xyz"


def test_event_only_failed_scheduling_warning():
    ev = {
        "type": "Warning",
        "reason": "FailedScheduling",
        "message": "0/1 nodes",
        "involvedObject": {"kind": "Pod", "name": "p"},
    }
    s = d.event_incident_spec("demo", ev, pod([]), apps_api())
    assert s["source"]["classHint"] == "UNSCHEDULABLE" and s["source"]["owner"] == "Deployment/api"
    assert s["fp"] == dedupe_fingerprint("demo", "Deployment/api", "", "FailedScheduling")
    assert d.event_incident_spec("demo", {**ev, "type": "Normal"}, None) is None
    assert d.event_incident_spec("demo", {**ev, "reason": "FailedMount"}, None) is None
    assert d.event_incident_spec("demo", ev, None)["source"]["owner"] == "Pod/p"


def test_rerun_failure_routes_to_incident():
    api = MagicMock()
    api.get_namespaced_custom_object.return_value = {"status": {"reviewFeedback": ["earlier"]}}
    patch = d.route_workflow_failure(api, "demo", "ri-abc", "build-xyz")
    assert patch["status"]["phase"] == "Analyzing"
    assert patch["status"]["reviewFeedback"] == ["earlier", "re-run failed"]
    args = api.patch_namespaced_custom_object_status.call_args.args
    assert args[4] == "ri-abc" and args[5] is patch
    api.create_namespaced_custom_object.assert_not_called()
