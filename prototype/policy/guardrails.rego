# Guardrail policy for repair proposals (CONTRACTS section 8, check 6).
#
# Input: one Kubernetes document (conftest splits multi-document YAML).
# Original file: guardrails.py parses the pre-edit file, writes
# {"original": [<doc>, ...]} to a JSON file in a temp dir and passes that dir with
# `conftest test --data <dir>`, so the rules compare against data.original.
# With no original data, comparisons find nothing to compare with (new RBAC
# objects are still denied; the secret rule treats every literal as new).
package main

import rego.v1

workload_kinds := {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}

argo_kinds := {"WorkflowTemplate", "Workflow", "ClusterWorkflowTemplate", "CronWorkflow"}

rbac_kinds := {"ServiceAccount", "Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}

default originals := []

originals := data.original

# ---- helpers: pod specs and containers --------------------------------------

pod_specs(doc) := {s |
	doc.kind in workload_kinds
	s := doc.spec.template.spec
} | {s |
	doc.kind == "CronJob"
	s := doc.spec.jobTemplate.spec.template.spec
} | {s |
	doc.kind == "Pod"
	s := doc.spec
} | {t |
	doc.kind in argo_kinds
	some t in doc.spec.templates
}

containers(doc) := {c |
	some s in pod_specs(doc)
	some c in s.containers
} | {c |
	some s in pod_specs(doc)
	some c in s.initContainers
} | {c |
	some s in pod_specs(doc)
	c := s.container
} | {c |
	some s in pod_specs(doc)
	c := s.script
}

same_object(a, b) if {
	a.kind == b.kind
	a.metadata.name == b.metadata.name
}

has_original if {
	some o in originals
	same_object(o, input)
}

orig_containers(doc) := {c |
	some o in originals
	same_object(o, doc)
	some c in containers(o)
}

orig_container(doc, name) := c if {
	some c in orig_containers(doc)
	c.name == name
}

registry(image) := "docker.io" if {
	not contains(image, "/")
}

registry(image) := first if {
	first := split(image, "/")[0]
	contains(image, "/")
	regex.match(`[.:]|^localhost$`, first)
}

registry(image) := "docker.io" if {
	first := split(image, "/")[0]
	contains(image, "/")
	not regex.match(`[.:]|^localhost$`, first)
}

# ---- privilege escalation ----------------------------------------------------

deny contains msg if {
	some c in containers(input)
	c.securityContext.privileged == true
	msg := sprintf("%s/%s: container %q must not be privileged", [input.kind, input.metadata.name, c.name])
}

deny contains msg if {
	some s in pod_specs(input)
	some field in ["hostPID", "hostNetwork", "hostIPC"]
	s[field] == true
	msg := sprintf("%s/%s: %s is not allowed", [input.kind, input.metadata.name, field])
}

deny contains msg if {
	some s in pod_specs(input)
	some v in s.volumes
	v.hostPath
	msg := sprintf("%s/%s: hostPath volume %q is not allowed", [input.kind, input.metadata.name, v.name])
}

deny contains msg if {
	some c in containers(input)
	count(c.securityContext.capabilities.add) > 0
	msg := sprintf("%s/%s: container %q adds capabilities", [input.kind, input.metadata.name, c.name])
}

# ---- RBAC / identity ---------------------------------------------------------

deny contains msg if {
	input.kind in rbac_kinds
	not has_original
	msg := sprintf("new %s/%s is not allowed", [input.kind, input.metadata.name])
}

deny contains msg if {
	some o in originals
	same_object(o, input)
	some s in pod_specs(input)
	some os in pod_specs(o)
	object.get(s, "serviceAccountName", "") != object.get(os, "serviceAccountName", "")
	msg := sprintf("%s/%s: serviceAccountName changed", [input.kind, input.metadata.name])
}

# ---- resource limits: at most 4x in one step ----------------------------------

limit_value(v) := units.parse(v) if is_string(v)

limit_value(v) := v if is_number(v)

deny contains msg if {
	some c in containers(input)
	oc := orig_container(input, c.name)
	some res in ["memory", "cpu"]
	new := limit_value(c.resources.limits[res])
	old := limit_value(oc.resources.limits[res])
	old > 0
	new > 4 * old
	msg := sprintf("%s/%s: container %q %s limit raised more than 4x", [input.kind, input.metadata.name, c.name, res])
}

# ---- image registry ----------------------------------------------------------

deny contains msg if {
	some c in containers(input)
	oc := orig_container(input, c.name)
	registry(c.image) != registry(oc.image)
	msg := sprintf("%s/%s: container %q image registry changed (%s -> %s)", [input.kind, input.metadata.name, c.name, registry(oc.image), registry(c.image)])
}

# ---- secret-looking literals --------------------------------------------------

secret_value_patterns := [
	`AKIA[0-9A-Z]{16}`,
	`gh[pousr]_[A-Za-z0-9]{20,}`,
	`sk-[A-Za-z0-9_-]{20,}`,
	`-----BEGIN [A-Z ]*PRIVATE KEY-----`,
	`(?i)bearer\s+[A-Za-z0-9._-]{20,}`,
	`xox[baprs]-[A-Za-z0-9-]{10,}`,
]

secret_name_pattern := `(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)`

secret_literals(doc) := {s |
	walk(doc, [_, s])
	is_string(s)
	some p in secret_value_patterns
	regex.match(p, s)
} | {sprintf("env:%s=%s", [e.name, e.value]) |
	some c in containers(doc)
	some e in c.env
	regex.match(secret_name_pattern, e.name)
	is_string(e.value)
	e.value != ""
}

original_secrets := {s |
	some o in originals
	some s in secret_literals(o)
}

deny contains msg if {
	some s in secret_literals(input)
	not s in original_secrets
	msg := sprintf("%s/%s: secret-looking literal (%s...)", [input.kind, input.metadata.name, substring(s, 0, 8)])
}

# ---- probes -------------------------------------------------------------------

deny contains msg if {
	some c in containers(input)
	oc := orig_container(input, c.name)
	some probe in ["readinessProbe", "livenessProbe"]
	oc[probe]
	not c[probe]
	msg := sprintf("%s/%s: container %q lost its %s", [input.kind, input.metadata.name, c.name, probe])
}
