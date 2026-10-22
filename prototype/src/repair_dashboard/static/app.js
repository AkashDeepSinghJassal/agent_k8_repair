"use strict";
// Vanilla JS, no dependencies. All dynamic text goes through textContent (no innerHTML).

const STEPS = ["Detected", "Collected", "Analyzing", "Proposed", "AwaitingReview", "Merged", "Verifying", "Healed"];
const STEP_LABEL = { AwaitingReview: "Awaiting review" };
const TERMINAL_OFF_PATH = { Dismissed: "neutral", Failed: "bad", Escalated: "bad" };
// Furthest step reached, inferred from timestamps when an incident ends off the happy path.
const TS_STEP = { detected: 0, collected: 1, analyzed: 2, proposed: 3, prOpened: 4, merged: 5, synced: 6, verified: 7 };

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  let body = null;
  try { body = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) {
    const err = new Error((body && (body.detail || body.error)) || `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return body;
}

async function postJSON(url, payload) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}),
  });
  let body = null;
  try { body = await r.json(); } catch (e) { /* non-JSON error body */ }
  if (!r.ok) {
    const d = body && (body.detail || body.error);
    const err = new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${r.status}`);
    err.status = r.status;
    throw err;
  }
  return body;
}

// ------------------------------------------------------------ toasts, log, confirm
function logActivity(msg, isErr) {
  const list = $("activity");
  if (list.firstChild && list.firstChild.classList.contains("muted")) list.replaceChildren();
  list.prepend(h("li", { class: isErr ? "err" : "" }, `${new Date().toLocaleTimeString()}  ${msg}`));
  while (list.children.length > 50) list.lastChild.remove();
}
function toast(msg, isErr) {
  const t = h("div", { class: "toast" + (isErr ? " err" : "") }, msg);
  $("toasts").append(t);
  setTimeout(() => t.remove(), isErr ? 9000 : 5000);
  logActivity(msg, isErr);
}
function confirmDialog(title, bodyNodes, okLabel) {
  const dlg = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-body").replaceChildren(...bodyNodes);
  $("confirm-ok").textContent = okLabel || "Confirm";
  return new Promise((resolve) => {
    const done = (v) => { dlg.close(); $("confirm-ok").onclick = $("confirm-cancel").onclick = dlg.oncancel = null; resolve(v); };
    $("confirm-ok").onclick = () => done(true);
    $("confirm-cancel").onclick = () => done(false);
    dlg.oncancel = (e) => { e.preventDefault(); done(false); };
    dlg.showModal();
    $("confirm-cancel").focus();
  });
}
// Run an async action with the button disabled; show errors inline (errBox) and as a toast.
async function runAction(btn, errBox, label, fn) {
  btn.disabled = true;
  if (errBox) errBox.textContent = "";
  try {
    const res = await fn();
    toast(`${label}: done`);
    return res;
  } catch (e) {
    if (errBox) errBox.textContent = `${label} failed: ${e.message}`;
    toast(`${label} failed: ${e.message}`, true);
    return null;
  } finally {
    btn.disabled = false;
  }
}

const ago = (iso) => {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (isNaN(s)) return iso;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};
const when = (iso) => (iso ? h("time", { datetime: iso, title: iso }, ago(iso)) : "-");

function badge(text, tone) { return h("span", { class: "badge " + (tone || "") }, text); }
const emptyBox = (msg) => h("div", { class: "empty" }, msg);
const errorBox = (what, err) => h("div", { class: "error", role: "alert" }, `Could not load ${what}: ${err.message}`);

function toneFor(value) {
  if (["Healthy", "Synced", "Succeeded", "Healed", "green"].includes(value)) return "green";
  if (["Failed", "Error", "Degraded", "Escalated", "red", "Missing"].includes(value)) return "red";
  if (["Progressing", "OutOfSync", "Running", "Suspended", "Pending", "amber", "Unknown"].includes(value)) return "amber";
  return "";
}

// ------------------------------------------------------------ services
function renderServices(list) {
  const root = $("services");
  root.replaceChildren();
  if (!list.length) return root.append(emptyBox("No services found in the demo namespaces yet."));
  const byNs = {};
  for (const s of list) (byNs[s.namespace] ||= []).push(s);
  for (const ns of Object.keys(byNs).sort()) {
    root.append(h("div", { class: "ns-title" }, "namespace ", h("code", {}, ns)));
    root.append(h("div", { class: "cards" }, byNs[ns].map(serviceCard)));
  }
}

function serviceCard(s) {
  const restarts = s.pods.reduce((n, p) => n + p.restarts, 0);
  const reason = s.pods.map((p) => p.lastReason).filter(Boolean).pop();
  const svc = s.service
    ? `${s.service.type} ${s.service.ports.map((p) => (p.nodePort ? `${p.port}:${p.nodePort}` : p.port)).join(", ")}`
    : "none";
  return h("article", { class: "card " + s.status, "aria-label": `${s.name}, status ${s.status}` },
    h("div", { class: "card-head" },
      h("span", { class: "card-title" }, s.name),
      badge(`${s.ready}/${s.desired} ready`, s.status)),
    h("dl", { class: "kv" },
      h("dt", {}, "Status"), h("dd", {}, s.status === "green" ? "Healthy" : s.status === "amber" ? "Degraded" : "Down"),
      h("dt", {}, "Version"), h("dd", {}, s.version || "-"),
      h("dt", {}, "Image"), h("dd", { class: "mono" }, s.image || "-"),
      h("dt", {}, "Restarts"), h("dd", {}, restarts),
      h("dt", {}, "Last reason"), h("dd", {}, reason || "-"),
      h("dt", {}, "Service"), h("dd", {}, svc)),
    s.pods.length
      ? h("ul", { class: "pods", "aria-label": "Pods" }, s.pods.map((p) =>
          h("li", {}, h("span", { class: "mono" }, p.name), badge(p.phase, toneFor(p.phase) || (p.ready ? "green" : "amber")))))
      : null);
}

// ------------------------------------------------------------ tables
function table(caption, cols, rows) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("caption", { class: "skip" }, caption),
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", { scope: "col" }, c)))),
    h("tbody", {}, rows)));
}

function renderGitops(apps) {
  const root = $("gitops");
  root.replaceChildren();
  if (!apps.length) return root.append(emptyBox("No Argo CD applications found."));
  root.append(table("Argo CD applications", ["Application", "Sync", "Health", "Revision", "Last sync"],
    apps.map((a) => h("tr", {},
      h("td", {}, a.name), h("td", {}, badge(a.sync, toneFor(a.sync))), h("td", {}, badge(a.health, toneFor(a.health))),
      h("td", { class: "mono" }, a.shortRevision || "-"), h("td", {}, when(a.lastSync))))));
}

function renderPipelines(wfs) {
  const root = $("pipelines");
  root.replaceChildren();
  if (!wfs.length) return root.append(emptyBox("No workflow runs yet."));
  root.append(table("Recent workflow runs", ["Run", "Template", "Phase", "Started", "Failure"],
    wfs.map((w) => h("tr", {},
      h("td", { class: "mono" }, w.name), h("td", {}, w.template || "-"), h("td", {}, badge(w.phase, toneFor(w.phase))),
      h("td", {}, when(w.startedAt)), h("td", {}, w.failedMessage || "-")))));
}

// ------------------------------------------------------------ faults
const faultErrors = {};
const faultBusy = new Set();

function faultCard(f) {
  const err = h("p", { class: "inline-error", role: "alert", id: `fault-err-${f.id}` }, faultErrors[f.id] || "");
  const mk = (label, verb) => {
    const b = h("button", { type: "button", class: "btn" + (verb === "inject" ? " btn-danger" : ""), "aria-describedby": `fault-err-${f.id}` }, label);
    b.disabled = faultBusy.has(f.id) || (verb === "revert" && !f.injected && f.kind !== "cluster");
    b.addEventListener("click", async () => {
      const ok = await confirmDialog(`${label} ${f.id}?`, [
        h("p", {}, h("strong", {}, f.title)), h("p", {}, f.description),
        h("p", {}, verb === "inject" ? "This changes the demo repo or cluster and will trigger the repair loop." : "This restores the healthy configuration."),
      ], label);
      if (!ok) return;
      faultBusy.add(f.id); faultErrors[f.id] = ""; b.disabled = true;
      const res = await runAction(b, err, `${label} ${f.id}`, () => postJSON(`/api/faults/${f.id}/${verb}`));
      faultBusy.delete(f.id);
      if (res === null) faultErrors[f.id] = err.textContent;
      else if (res.status === "already-injected") toast(`${f.id} was already injected`);
      refreshFaults();
    });
    return b;
  };
  return h("article", { class: "card" + (f.injected ? " injected" : ""), "aria-label": `Fault ${f.id}: ${f.title}` },
    h("div", { class: "card-head" }, h("span", { class: "card-title" }, `${f.id} ${f.title}`),
      badge(f.injected ? "injected" : "clean", f.injected ? "red" : "green")),
    h("p", {}, f.description),
    h("dl", { class: "kv" },
      h("dt", {}, "Kind"), h("dd", {}, `${f.kind} in ${f.namespace}`),
      h("dt", {}, "Expected class"), h("dd", {}, f.expected_class),
      h("dt", {}, "Expected outcome"), h("dd", {}, f.expected_outcome),
      h("dt", {}, "Correct fix"), h("dd", {}, f.correct_fix),
      h("dt", {}, "Incident"), h("dd", {}, f.incident || "-")),
    h("div", { class: "card-actions" }, mk("Inject", "inject"), mk("Revert", "revert")),
    err);
}

async function refreshFaults() {
  const root = $("fault-panel");
  try {
    const list = await getJSON("/api/faults");
    root.replaceChildren(...list.map(faultCard));
  } catch (e) {
    root.replaceChildren(errorBox("faults", e));
  }
  root.setAttribute("aria-busy", "false");
  try {
    const st = await getJSON("/api/actions/status");
    const banner = $("gh-banner");
    banner.hidden = st.github;
    banner.textContent = st.github ? "" : st.message;
  } catch (e) { /* ignore */ }
}

// ------------------------------------------------------------ incidents
function stepBar(inc) {
  const off = TERMINAL_OFF_PATH[inc.phase];
  let idx = STEPS.indexOf(inc.phase);
  if (off) {
    idx = 0;
    for (const [k, v] of Object.entries(TS_STEP)) if (inc.timestamps && inc.timestamps[k]) idx = Math.max(idx, v);
  }
  const label = off ? `${inc.phase} (after ${STEPS[idx]})` : STEP_LABEL[inc.phase] || inc.phase;
  const items = STEPS.map((s, i) => {
    const state = i < idx || (inc.phase === "Healed" && i <= idx) ? "done" : i === idx ? "current" : "";
    return h("li", { class: state, title: STEP_LABEL[s] || s });
  });
  return h("div", {},
    h("ol", { class: "steps " + (off ? TERMINAL_OFF_PATH[inc.phase] : ""), "aria-label": `Progress: ${label}` }, items),
    h("div", { class: "step-label" }, label));
}

function renderIncidents(list) {
  const root = $("incident-list");
  root.replaceChildren();
  if (!list.length) return root.append(emptyBox("No repair incidents. The loop is idle, or nothing has failed yet."));
  root.append(table("Repair incidents", ["Incident", "Class", "Tier", "Progress", "PR", "Attempts", "Opened"],
    list.map((i) => h("tr", {},
      h("td", {}, h("button", { type: "button", class: "linkish", onclick: () => openDrawer(i.namespace, i.name) }, i.name),
        h("div", { class: "muted mono" }, i.namespace)),
      h("td", {}, i.class || "-", i.shadow ? h("div", {}, badge("shadow")) : null),
      h("td", {}, i.tier ?? "-"),
      h("td", {}, stepBar(i)),
      h("td", {}, i.pullRequest ? h("a", { href: i.pullRequest, target: "_blank", rel: "noopener" }, "PR") : "-"),
      h("td", {}, i.attempts),
      h("td", {}, when(i.createdAt))))));
}

// ------------------------------------------------------------ drawer
let drawerOpener = null;
async function openDrawer(ns, name) {
  drawerOpener = document.activeElement;
  const d = $("drawer"), body = $("drawer-body");
  $("drawer-title").textContent = `${name} (${ns})`;
  body.replaceChildren(h("p", { class: "muted" }, "Loading..."));
  d.hidden = false;
  $("drawer-close").focus();
  try {
    body.replaceChildren(...drawerContent(await getJSON(`/api/incidents/${encodeURIComponent(ns)}/${encodeURIComponent(name)}`)));
  } catch (e) {
    body.replaceChildren(errorBox("incident", e));
  }
}
function closeDrawer() {
  $("drawer").hidden = true;
  if (drawerOpener && drawerOpener.isConnected) drawerOpener.focus();
}

function drawerContent(i) {
  const p = i.proposal;
  const out = [
    stepBar(i),
    h("dl", { class: "kv" },
      h("dt", {}, "Class"), h("dd", {}, i.class ? `${i.class} (${i.confidence ?? "?"}, via ${i.source || "?"})` : "-"),
      h("dt", {}, "Outcome"), h("dd", {}, i.outcome ? `${i.outcome.result}${i.outcome.reason ? ": " + i.outcome.reason : ""}` : "in progress"),
      h("dt", {}, "Attempts"), h("dd", {}, i.attempts),
      h("dt", {}, "Signature"), h("dd", { class: "mono" }, i.signature || "-"),
      h("dt", {}, "Evidence"), h("dd", { class: "mono" }, i.evidenceRef || "-"),
      h("dt", {}, "Pull request"), h("dd", {}, i.pullRequest ? h("a", { href: i.pullRequest, target: "_blank", rel: "noopener" }, i.pullRequest) : "-")),
    h("h4", {}, "Timestamps"),
    h("ul", { class: "ts" }, Object.keys(TS_STEP).concat(["closed"]).map((k) =>
      h("li", {}, h("span", {}, k), h("span", { class: "mono" }, (i.timestamps && i.timestamps[k]) || "-")))),
    h("h4", {}, "Proposal"),
  ];
  if (!p) out.push(h("p", { class: "muted" }, "No proposal yet."));
  else {
    out.push(h("dl", { class: "kv" },
      h("dt", {}, "Tier"), h("dd", {}, p.tier),
      h("dt", {}, "File"), h("dd", { class: "mono" }, p.file || "-"),
      h("dt", {}, "Template"), h("dd", {}, p.template ? `${p.template} ${JSON.stringify(p.params || {})}` : "-"),
      h("dt", {}, "Re-measure"), h("dd", {}, p.needs_remeasure ? "yes" : "no"),
      h("dt", {}, "Rejected by"), h("dd", {}, p.rejectedBy || "-")));
    if (p.rationale) out.push(h("p", {}, p.rationale));
    for (const e of p.edits || [])
      out.push(h("pre", { class: "diff" }, `- ${e.find}\n+ ${e.replace}`));
  }
  if (i.reviewFeedback && i.reviewFeedback.length)
    out.push(h("h4", {}, "Review feedback"), h("ul", {}, i.reviewFeedback.map((f) => h("li", {}, f))));
  if (i.pullRequest) out.push(...prActions(i));
  return out;
}

function patchNode(patch) {
  const pre = h("pre", { class: "diff patch", tabindex: "0" });
  for (const line of (patch || "(no textual diff)").split("\n")) {
    const cls = line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    pre.append(h("span", { class: cls }, line + "\n"));
  }
  return pre;
}
function diffNodes(data) {
  return data.files.flatMap((f) => [
    h("p", { class: "mono" }, `${f.filename} (${f.status}, +${f.additions} -${f.deletions})`), patchNode(f.patch)]);
}

function prActions(i) {
  const base = `/api/incidents/${encodeURIComponent(i.namespace)}/${encodeURIComponent(i.name)}`;
  const err = h("p", { class: "inline-error", role: "alert" });
  const diffBox = h("div", { "aria-live": "polite" });
  const viewBtn = h("button", { type: "button", class: "btn" }, "View diff");
  const approveBtn = h("button", { type: "button", class: "btn btn-primary" }, "Approve & merge");
  const reviewable = i.phase === "AwaitingReview";
  approveBtn.disabled = !reviewable;
  if (!reviewable) approveBtn.title = `Only available in phase AwaitingReview (now ${i.phase})`;
  const loadDiff = async () => { const d = await getJSON(`${base}/diff`); diffBox.replaceChildren(...diffNodes(d)); return d; };
  viewBtn.addEventListener("click", () => runAction(viewBtn, err, "Load diff", loadDiff));
  approveBtn.addEventListener("click", async () => {
    approveBtn.disabled = true; err.textContent = "";
    let data;
    try { data = await loadDiff(); } catch (e) { err.textContent = `Load diff failed: ${e.message}`; approveBtn.disabled = !reviewable; return; }
    const ok = await confirmDialog("Approve and squash-merge?", [
      h("p", {}, `Merge ${i.pullRequest} into the demo repo. Review the diff first.`), ...diffNodes(data)], "Approve & merge");
    if (!ok) { approveBtn.disabled = !reviewable; return; }
    const res = await runAction(approveBtn, err, "Approve & merge", () => postJSON(`${base}/approve`, { confirm: true }));
    if (res) {
      if (res.warning) toast(res.warning, true);
      approveBtn.disabled = true;
      refresh();
    } else approveBtn.disabled = !reviewable;
  });
  const text = h("textarea", { rows: 3, id: "rc-text", placeholder: "What should the next attempt change?", "aria-label": "Requested changes" });
  const rcBtn = h("button", { type: "button", class: "btn" }, "Request changes");
  rcBtn.addEventListener("click", async () => {
    if (!text.value.trim()) { err.textContent = "Describe the changes you want first."; text.focus(); return; }
    const res = await runAction(rcBtn, err, "Request changes", () => postJSON(`${base}/request-changes`, { text: text.value }));
    if (res) text.value = "";
  });
  return [
    h("h4", {}, "Review"),
    h("div", { class: "drawer-actions" },
      h("div", { class: "row" }, viewBtn, approveBtn), diffBox,
      h("label", { for: "rc-text" }, "Request changes (posts /repair retry on the PR)"), text, h("div", { class: "row" }, rcBtn), err),
  ];
}

// ------------------------------------------------------------ refresh loop
async function section(url, render, what, rootId) {
  try {
    render(await getJSON(url));
    return true;
  } catch (e) {
    $(rootId).replaceChildren(e.status === 503
      ? emptyBox(`No ${what} to show: the cluster is not reachable yet (see the header). This page keeps retrying.`)
      : errorBox(what, e));
    return false;
  }
}

async function refresh() {
  let health = null;
  try { health = await getJSON("/api/health"); } catch (e) { health = { cluster: false, detail: e.message }; }
  const dot = $("cluster-dot");
  dot.className = "dot " + (health.cluster ? "dot-ok" : "dot-bad");
  $("cluster-text").textContent = health.cluster
    ? `Cluster connected (${health.context})`
    : `Cluster unreachable${health.context ? " (" + health.context + ")" : ""}${health.detail ? ": " + health.detail : ""}`;
  await Promise.all([
    section("/api/services", renderServices, "services", "services"),
    section("/api/argocd", renderGitops, "Argo CD apps", "gitops"),
    section("/api/workflows", renderPipelines, "workflows", "pipelines"),
    section("/api/incidents", renderIncidents, "incidents", "incident-list"),
  ]);
  $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
}

$("refresh").addEventListener("click", refresh);
$("rerun").addEventListener("click", async () => {
  const ok = await confirmDialog("Re-run pipeline?", [h("p", {}, "Submit a new release-pipeline run in namespace demo.")], "Re-run");
  if (!ok) return;
  const msg = $("rerun-msg");
  const res = await runAction($("rerun"), msg, "Re-run pipeline", () => postJSON("/api/pipelines/rerun"));
  if (res) { msg.textContent = `Submitted ${res.name}`; refresh(); }
});
$("drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) closeDrawer(); });
refresh();
refreshFaults();
setInterval(() => { if (!document.hidden) { refresh(); refreshFaults(); } }, 5000);
