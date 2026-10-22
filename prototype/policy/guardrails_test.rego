package main_test

import rego.v1

import data.main

container(extra) := object.union({
	"name": "api",
	"image": "python:3.12-alpine",
	"readinessProbe": {"httpGet": {"path": "/healthz", "port": 8000}},
	"livenessProbe": {"httpGet": {"path": "/healthz", "port": 8000}},
	"resources": {"limits": {"memory": "128Mi", "cpu": "500m"}},
}, extra)

deployment(c, pod_extra) := {
	"apiVersion": "apps/v1",
	"kind": "Deployment",
	"metadata": {"name": "api"},
	"spec": {"template": {"spec": object.union({"containers": [c]}, pod_extra)}},
}

base := deployment(container({}), {})

denied_with(doc, orig) := r if {
	r := main.deny with input as doc with data.original as orig
}

denied_no_data(doc) := r if {
	r := main.deny with input as doc
}

denied(doc) := denied_with(doc, [base])

test_unchanged_passes if {
	count(denied(base)) == 0
}

test_memory_bump_2x_passes if {
	count(denied(deployment(container({"resources": {"limits": {"memory": "256Mi", "cpu": "500m"}}}), {}))) == 0
}

test_memory_over_4x_denied if {
	d := denied(deployment(container({"resources": {"limits": {"memory": "1Gi", "cpu": "500m"}}}), {}))
	count(d) == 1
}

test_cpu_over_4x_denied if {
	count(denied(deployment(container({"resources": {"limits": {"memory": "128Mi", "cpu": "4"}}}), {}))) == 1
}

test_privileged_denied if {
	count(denied(deployment(container({"securityContext": {"privileged": true}}), {}))) == 1
}

test_host_pid_network_denied if {
	count(denied(deployment(container({}), {"hostPID": true, "hostNetwork": true}))) == 2
}

test_hostpath_denied if {
	count(denied(deployment(container({}), {"volumes": [{"name": "h", "hostPath": {"path": "/"}}]}))) == 1
}

test_added_capability_denied if {
	count(denied(deployment(container({"securityContext": {"capabilities": {"add": ["SYS_ADMIN"]}}}), {}))) == 1
}

test_dropped_capability_ok if {
	count(denied(deployment(container({"securityContext": {"capabilities": {"drop": ["ALL"]}}}), {}))) == 0
}

test_new_serviceaccount_object_denied if {
	sa := {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "evil"}}
	count(denied_with(sa, [base])) == 1
}

test_new_clusterrole_denied_without_original if {
	cr := {"kind": "ClusterRole", "metadata": {"name": "x"}}
	count(denied_no_data(cr)) == 1
}

test_existing_rbac_object_ok if {
	sa := {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "app"}}
	count(denied_with(sa, [sa])) == 0
}

test_changed_service_account_denied if {
	count(denied(deployment(container({}), {"serviceAccountName": "admin"}))) == 1
}

test_registry_change_denied if {
	count(denied(deployment(container({"image": "evil.example.com/python:3.12-alpine"}), {}))) == 1
}

test_same_registry_tag_change_ok if {
	count(denied(deployment(container({"image": "python:3.13-alpine"}), {}))) == 0
}

test_secret_literal_denied if {
	d := denied(deployment(container({"env": [{"name": "DB_PASSWORD", "value": "hunter2"}]}), {}))
	count(d) == 1
}

test_token_value_denied if {
	d := denied(deployment(container({"env": [{"name": "X", "value": "AKIAABCDEFGHIJKLMNOP"}]}), {}))
	count(d) == 1
}

test_secret_ref_ok if {
	env := [{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]
	count(denied(deployment(container({"env": env}), {}))) == 0
}

test_removed_readiness_denied if {
	c := object.remove(container({}), ["readinessProbe"])
	count(denied(deployment(c, {}))) == 1
}

test_removed_liveness_denied if {
	c := object.remove(container({}), ["livenessProbe"])
	count(denied(deployment(c, {}))) == 1
}

test_workflow_template_privileged_denied if {
	wf := {"kind": "WorkflowTemplate", "metadata": {"name": "w"}, "spec": {"templates": [{"name": "t", "container": {"name": "main", "image": "alpine:3.20", "securityContext": {"privileged": true}}}]}}
	count(denied_with(wf, [])) == 1
}
