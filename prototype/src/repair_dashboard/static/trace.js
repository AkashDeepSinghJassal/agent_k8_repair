"use strict";
// Trace pop-up, Recent flows and Knowledge views. Loaded after app.js and reuses its helpers
// (h, getJSON, badge, ago, when, table, emptyBox, errorBox, toast). Text goes through textContent only.

// ------------------------------------------------------------ vocabulary
const STAGES = [
  { id: "detected", label: "Detected", kinds: ["detected"] },
  { id: "evidence", label: "Evidence", kinds: ["evidence"] },
  { id: "route", label: "Route", kinds: ["route"] },
  { id: "work", label: "Investigate / Replay", kinds: ["agent_start", "agent_end", "tool_call", "llm_call", "extract"] },
  { id: "proposal", label: "Proposal", kinds: ["proposal"] },
  { id: "guardrail", label: "Guardrails", kinds: ["guardrail"] },
  { id: "pr", label: "PR", kinds: ["pr"] },
  { id: "review", label: "Review", kinds: ["review"] },
  { id: "sync", label: "Sync", kinds: ["sync"] },
  { id: "verify", label: "Verify", kinds: ["verify"] },
  { id: "outcome", label: "Outcome", kinds: ["outcome"] },
  { id: "learn", label: "Learn", kinds: ["learn"] },
];
const stageOf = (kind) => (STAGES.find((s) => s.kinds.includes(kind)) || STAGES[3]).id;
const KIND_ICON = {
  detected: "!", evidence: "▤", route: "⑂", agent_start: "▶", agent_end: "■", tool_call: "⚙", llm_call: "✎",
  extract: "⇥", proposal: "✚", guardrail: "⛨", pr: "⇄", review: "☑", sync: "↻", verify: "♥", outcome: "★", learn: "✦",
};
const TIERS = {
  T1: { tone: "t1", label: "Deterministic · no LLM" },
  T2: { tone: "t2", label: "Hybrid · 1 LLM call" },
  T3: { tone: "t3", label: "Agent · MCP tools" },
};
const FILTERS = [
  { id: "all", label: "All events", test: () => true },
  { id: "tools", label: "Tools only", test: (e) => e.kind === "tool_call" },
  { id: "llm", label: "LLM only", test: (e) => e.kind === "llm_call" || e.kind === "agent_start" || e.kind === "agent_end" },
  { id: "learn", label: "Learning only", test: (e) => e.kind === "learn" },
];

const tierBadge = (tier, short) => {
  const t = TIERS[tier];
  return t ? h("span", { class: "tier " + t.tone, title: t.label }, short ? tier : `${tier} ${t.label}`) : null;
};
const fmtMs = (ms) => (ms == null ? "" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`);
const fmtOffset = (ms) => `+${(ms / 1000).toFixed(1)}s`;
const shortSig = (s) => (s ? String(s).slice(0, 12) : "-");

function pretty(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") {
    const t = value.trim();
    if (t.startsWith("{") || t.startsWith("[")) {
      try { return JSON.stringify(JSON.parse(t), null, 2); } catch (e) { /* plain text */ }
    }
    return value;
  }
  return JSON.stringify(value, null, 2);
}

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    const ta = h("textarea", { style: "position:fixed;left:-999px" }); ta.value = text;
    document.body.append(ta); ta.select();
    try { document.execCommand("copy"); } catch (e2) { /* ignore */ }
    ta.remove();
  }
  const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = old; }, 1200);
}

function pane(label, text) {
  const btn = h("button", { type: "button", class: "btn btn-small", onclick: (ev) => copyText(text, ev.currentTarget) }, "Copy");
  return h("div", { class: "pane" },
    h("div", { class: "pane-head" }, h("strong", {}, label), btn),
    h("pre", { class: "pane-body", tabindex: "0" }, text || "(empty)"));
}

// ------------------------------------------------------------ derived facts
// (a failed tool call is normal investigation, so it reddens its row but not the whole stage)
function stageStatus(events, stage, terminal) {
  const evs = events.filter((e) => stageOf(e.kind) === stage.id);
  if (!evs.length) return terminal ? "skipped" : "pending";
  if (evs.some((e) => (e.status === "error" && e.kind !== "tool_call") || (e.kind === "guardrail" && e.data && e.data.ok === false))) return "error";
  if (evs.some((e) => e.status === "running")) return "running";
  if (evs.every((e) => e.status === "skipped")) return "skipped";
  return "ok";
}

function flowFacts(events) {
  const first = events[0], last = events[events.length - 1];
  const t0 = first ? Date.parse(first.ts) : 0;
  const routeEv = events.find((e) => e.kind === "route");
  const propEv = events.find((e) => e.kind === "proposal");
  const outcome = events.find((e) => e.kind === "outcome");
  const det = events.find((e) => e.kind === "detected");
  let tokens = 0;
  for (const e of events) {
    const d = e.data || {};
    if (e.kind === "agent_end") tokens += (d.input_tokens || 0) + (d.output_tokens || 0);
    if (e.kind === "llm_call") {
      const t = d.tokens;
      tokens += typeof t === "number" ? t : t ? (t.input || 0) + (t.output || 0) : 0;
    }
  }
  const lastEv = events.length ? events.reduce((a, b) => (Date.parse(b.ts) >= Date.parse(a.ts) ? b : a)) : null;
  const tier = (routeEv && routeEv.data && (routeEv.data.chosen_tier ?? routeEv.data.tier ?? routeEv.data.chosen)) || (propEv && propEv.data && propEv.data.tier)
    || (events.find((e) => e.tier) || {}).tier || null;
  const fault = det && det.data && det.data.fault;
  const phase = outcome ? (outcome.data && outcome.data.phase) || outcome.title : null;
  const current = lastEv ? STAGES.find((s) => s.id === stageOf(lastEv.kind)).label : "";
  return {
    t0, tier, tokens, fault, outcome: phase,
    terminal: !!outcome,
    phase: phase || (current ? `In progress: ${current}` : "-"),
    duration: lastEv ? Date.parse(lastEv.ts) - t0 : 0,
  };
}

// ------------------------------------------------------------ event rows
function eventBody(e) {
  const d = e.data || {};
  const out = [];
  const kv = (rows) => h("dl", { class: "kv" }, rows.filter((r) => r[1] != null && r[1] !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", { class: "mono" }, typeof v === "string" ? v : JSON.stringify(v))]));
  switch (e.kind) {
    case "tool_call":
      out.push(kv([["server", d.server], ["tool", d.tool], ["step", d.step], ["ms", d.ms]]));
      if (d.error) out.push(h("div", { class: "inline-error", role: "alert" }, "Error: " + d.error));
      out.push(h("div", { class: "panes" }, pane("Input (args)", pretty(d.args)), pane("Output (result)", pretty(d.result))));
      break;
    case "llm_call":
      out.push(kv([["provider", d.provider], ["model", d.model], ["tokens", d.tokens], ["ms", d.ms]]));
      out.push(h("div", { class: "panes" }, pane("Prompt", pretty(d.prompt)), pane("Response", pretty(d.response))));
      break;
    case "extract":
      out.push(kv([["variable", d.variable], ["from step", d.from_step], ["rule", d.rule], ["value", d.value], ["error", d.error]]));
      break;
    case "proposal":
      out.push(kv([["tier", d.tier], ["class", d.class], ["confidence", d.confidence], ["file", d.file], ["root cause", d.root_cause], ["rationale", d.rationale]]));
      for (const ed of d.edits || []) out.push(h("pre", { class: "diff" }, `- ${ed.find}\n+ ${ed.replace}`));
      break;
    default:
      out.push(h("div", { class: "panes one" }, pane("Data", pretty(d))));
  }
  return out;
}

function inlineExtra(e) {
  const d = e.data || {};
  if (e.kind === "route") {
    const bits = [h("strong", {}, `Chosen tier: ${d.chosen_tier ?? d.tier ?? d.chosen ?? e.tier || "?"}`)];
    if (d.reason) bits.push(h("div", {}, d.reason));
    const meta = [d.playbook_id && `playbook ${d.playbook_id}`, d.playbook_state && `state ${d.playbook_state}`, d.fallback_from && `fallback from ${d.fallback_from}`,
      d.signature && `signature ${shortSig(d.signature)}`].filter(Boolean);
    if (meta.length) bits.push(h("div", { class: "muted" }, meta.join(" · ")));
    return h("div", { class: "callout route" }, bits);
  }
  if (e.kind === "guardrail") {
    const ok = d.ok !== false && e.status !== "error";
    return h("div", { class: "guard " + (ok ? "ok" : "bad") }, h("span", { class: "guard-mark", "aria-label": ok ? "passed" : "failed" }, ok ? "✓" : "✗"), d.detail || "");
  }
  if (e.kind === "learn") {
    const bits = [h("strong", {}, `${(d.action || "learn").toUpperCase()}${d.playbook_id ? " " + d.playbook_id : ""}`)];
    if (d.from_state || d.to_state) bits.push(` ${d.from_state || "-"} → ${d.to_state || "-"}`);
    if (d.reason) bits.push(h("div", {}, d.reason));
    if (d.counts) bits.push(h("div", { class: "muted mono" }, JSON.stringify(d.counts)));
    return h("div", { class: "callout learn" }, bits);
  }
  return null;
}

const searchText = (e) => (e.title + " " + e.kind + " " + JSON.stringify(e.data || {})).toLowerCase();

function eventRow(e, depth, t0, open, onToggle) {
  const extra = inlineExtra(e);
  const expandable = !["guardrail", "route", "learn"].includes(e.kind);
  const isOpen = expandable && open.has(e.id);
  const off = Date.parse(e.ts) - t0;
  const head = h(expandable ? "button" : "div", {
    class: "tl-head", type: expandable ? "button" : null, "aria-expanded": expandable ? String(isOpen) : null,
    onclick: expandable ? () => onToggle(e.id) : null,
  },
    h("span", { class: "tl-caret", "aria-hidden": "true" }, expandable ? (isOpen ? "▾" : "▸") : ""),
    h("span", { class: "tl-icon k-" + e.kind, "aria-hidden": "true", title: e.kind }, KIND_ICON[e.kind] || "•"),
    h("span", { class: "tl-title" }, e.title),
    e.status === "running" ? h("span", { class: "badge blue" }, "running") : e.status === "error" ? h("span", { class: "badge red" }, "error") : e.status === "skipped" ? h("span", { class: "badge" }, "skipped") : null,
    tierBadge(e.tier, true),
    h("span", { class: "tl-dur mono" }, fmtMs(e.duration_ms)),
    h("span", { class: "tl-off mono" }, fmtOffset(off)));
  return h("div", {
    class: `tl-row k-${e.kind} st-${e.status}${isOpen ? " open" : ""}`, "data-stage": stageOf(e.kind), id: "ev-" + e.id,
    style: `--depth:${depth}`,
  }, head, extra, isOpen ? h("div", { class: "tl-body" }, eventBody(e)) : null);
}

// ------------------------------------------------------------ modal
const T = { ns: null, name: null, events: [], open: new Set(), filter: "all", q: "", timer: null, extraPolls: 0, opener: null, lastSig: "" };

function openTrace(ns, name) {
  T.ns = ns; T.name = name; T.events = []; T.open = new Set(); T.filter = "all"; T.q = ""; T.lastSig = ""; T.extraPolls = 3;
  T.opener = document.activeElement;
  $("trace-search").value = "";
  $("trace-title").textContent = name;
  $("trace-sub").textContent = `namespace ${ns}`;
  $("trace-strip").replaceChildren();
  $("trace-timeline").replaceChildren(h("p", { class: "muted" }, "Loading..."));
  $("trace-filters").replaceChildren(...FILTERS.map((f) => {
    const b = h("button", { type: "button", class: "chip" + (f.id === "all" ? " on" : ""), "data-f": f.id, "aria-pressed": String(f.id === "all"), onclick: () => { T.filter = f.id; paintFilters(); paintTimeline(true); } }, f.label);
    return b;
  }));
  const dlg = $("trace");
  if (!dlg.open) dlg.showModal();
  pollTrace();
  clearInterval(T.timer);
  T.timer = setInterval(() => { if (!document.hidden) pollTrace(); }, 2000);
}

function closeTrace() {
  clearInterval(T.timer); T.timer = null;
  const dlg = $("trace");
  if (dlg.open) dlg.close();
  if (T.opener && T.opener.isConnected) T.opener.focus();
}

async function pollTrace() {
  const { ns, name } = T;
  let data;
  try { data = await getJSON(`/api/incidents/${encodeURIComponent(ns)}/${encodeURIComponent(name)}/flow`); }
  catch (e) { $("trace-timeline").replaceChildren(errorBox("flow", e)); return; }
  if (ns !== T.ns || name !== T.name) return;  // user opened another trace meanwhile
  T.events = data.events || [];
  const sig = T.events.map((e) => e.id + e.status + (e.duration_ms || "")).join("|");
  const changed = sig !== T.lastSig;
  T.lastSig = sig;
  paintHeader();
  if (changed) { paintStrip(); paintTimeline(false); }
  if (flowFacts(T.events).terminal) {
    if (T.extraPolls-- <= 0) { clearInterval(T.timer); T.timer = null; }
  }
}

function paintFilters() {
  for (const b of $("trace-filters").children) {
    const on = b.dataset.f === T.filter;
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on));
  }
}

function paintHeader() {
  const f = flowFacts(T.events);
  const live = !f.terminal && T.events.length;
  const tone = f.outcome === "Healed" ? "green" : f.outcome ? "red" : "blue";
  $("trace-meta").replaceChildren(...[
    f.fault ? h("span", { class: "badge" }, `Fault ${f.fault}`) : null,
    h("span", { class: "badge " + tone }, f.phase),
    f.tier ? tierBadge(f.tier) : h("span", { class: "badge" }, "tier unknown"),
    h("span", { class: "meta-item" }, h("span", { class: "muted" }, "Tokens "), h("strong", {}, f.tokens ? f.tokens.toLocaleString() : "0")),
    h("span", { class: "meta-item" }, h("span", { class: "muted" }, "Duration "), h("strong", {}, T.events.length ? fmtMs(f.duration) : "-")),
    h("span", { class: "meta-item" }, h("span", { class: "muted" }, "Events "), h("strong", {}, T.events.length)),
    live ? h("span", { class: "badge blue pulse" }, "live, refreshing every 2 s") : null].filter(Boolean));
}

function paintStrip() {
  const f = flowFacts(T.events);
  const items = STAGES.map((s) => {
    const st = stageStatus(T.events, s, f.terminal);
    const n = T.events.filter((e) => stageOf(e.kind) === s.id).length;
    return h("li", {}, h("button", {
      type: "button", class: "stage st-" + st, title: `${s.label}: ${st}${n ? ", " + n + " event(s)" : ""}`,
      disabled: n ? null : true,
      onclick: () => {
        const el = $("trace-timeline").querySelector(`[data-stage="${s.id}"]`);
        if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1400); }
      },
    }, h("span", { class: "stage-dot", "aria-hidden": "true" }), h("span", { class: "stage-label" }, s.label), h("span", { class: "stage-st" }, st)));
  });
  $("trace-strip").replaceChildren(...items);
}

function paintTimeline(keepScroll) {
  const box = $("trace-timeline");
  const scroll = box.scrollTop;
  const f = flowFacts(T.events);
  const test = FILTERS.find((x) => x.id === T.filter).test;
  const q = T.q.trim().toLowerCase();
  const byId = Object.fromEntries(T.events.map((e) => [e.id, e]));
  const depthOf = (e) => { let d = 0, p = e.parent && byId[e.parent]; while (p && d < 4) { d++; p = p.parent && byId[p.parent]; } return d; };
  const rows = T.events.filter((e) => test(e) && (!q || searchText(e).includes(q)));
  const toggle = (id) => { T.open.has(id) ? T.open.delete(id) : T.open.add(id); paintTimeline(true); };
  if (!T.events.length) box.replaceChildren(emptyBox("No flow recorded for this incident yet. It appears as soon as the operator emits its first event."));
  else if (!rows.length) box.replaceChildren(emptyBox("No events match the filter."));
  else box.replaceChildren(...rows.map((e) => eventRow(e, test === FILTERS[0].test ? depthOf(e) : 0, f.t0, T.open, toggle)));
  box.scrollTop = scroll;
}

// ------------------------------------------------------------ recent flows (tab)
async function loadRecentFlows() {
  const root = $("recent-flows");
  try {
    const list = await getJSON("/api/flows/recent?limit=20");
    if (!list.length) return root.replaceChildren(emptyBox("No flows recorded yet. Inject a fault and the trace appears here."));
    root.replaceChildren(table("Recent flows", ["Incident", "Fault", "Tier", "Outcome", "Tool calls", "Tokens", "Started", "Duration", ""],
      list.map((s) => h("tr", {},
        h("td", {}, h("span", { class: "mono" }, s.incident), h("div", { class: "muted mono" }, s.namespace)),
        h("td", {}, s.fault || "-"),
        h("td", {}, tierBadge(s.tier) || "-"),
        h("td", {}, s.outcome ? badge(s.outcome, toneFor(s.outcome)) : badge("in progress", "blue")),
        h("td", {}, s.steps), h("td", {}, (s.tokens || 0).toLocaleString()),
        h("td", {}, when(s.start)),
        h("td", { class: "mono" }, s.start && s.end ? fmtMs(Date.parse(s.end) - Date.parse(s.start)) : "-"),
        h("td", {}, h("button", { type: "button", class: "btn", onclick: () => openTrace(s.namespace, s.incident) }, "Open trace"))))));
  } catch (e) { root.replaceChildren(errorBox("recent flows", e)); }
}

// ------------------------------------------------------------ knowledge (tab)
let selectedPlaybook = null;
const lastHistory = (p) => (p.history && p.history.length ? p.history[p.history.length - 1] : null);

async function loadKnowledge() {
  const root = $("playbook-list");
  let list;
  try { list = await getJSON("/api/playbooks"); } catch (e) { return root.replaceChildren(errorBox("playbooks", e)); }
  if (!list.length) {
    root.replaceChildren(emptyBox("No playbooks yet. The first healed T3 agent run for a signature creates one."));
    return $("playbook-detail").replaceChildren();
  }
  const rows = list.map((p) => {
    const r = p.runs || {}, lh = lastHistory(p);
    const tr = h("tr", { class: "clickable" + (p.id === selectedPlaybook ? " sel" : ""), tabindex: "0", onclick: () => selectPlaybook(p.id, list), onkeydown: (ev) => { if (ev.key === "Enter") selectPlaybook(p.id, list); } },
      h("td", { class: "mono" }, shortSig(p.signature), h("div", { class: "muted" }, p.id)),
      h("td", {}, p.cls || "-"),
      h("td", {}, tierBadge(p.state) || p.state),
      h("td", { class: "mono" }, `T3 ${r.t3_ok || 0} · T2 ${r.t2_ok || 0} · T1 ${r.t1_ok || 0} · fail ${r.fail || 0}`),
      h("td", {}, p.consistency == null ? "-" : `${Math.round(p.consistency * 100)}%`),
      h("td", {}, h("div", { class: "chips" }, (p.action_sequence || []).map((a) => h("span", { class: "chip static mono" }, a)))),
      h("td", {}, lh ? [h("strong", {}, lh.event), " ", h("span", { class: "muted" }, ago(lh.ts))] : "-"));
    return tr;
  });
  root.replaceChildren(table("Playbooks", ["Signature", "Class", "State", "Runs", "Consistency", "Action sequence", "Last event"], rows));
  if (selectedPlaybook) {
    const p = list.find((x) => x.id === selectedPlaybook);
    if (p) paintPlaybook(p);
  }
}

function selectPlaybook(id, list) {
  selectedPlaybook = id;
  for (const tr of $("playbook-list").querySelectorAll("tr.clickable")) tr.classList.remove("sel");
  paintPlaybook(list.find((x) => x.id === id));
  loadKnowledge();
}

function paintPlaybook(p) {
  const box = $("playbook-detail");
  const r = p.runs || {};
  const tmpl = (p.edits_template || []).map((ed) => h("pre", { class: "diff" }, `- ${ed.find}\n+ ${ed.replace}`));
  box.replaceChildren(
    h("div", { class: "detail-head" }, h("h3", {}, `Playbook ${p.id}`), tierBadge(p.state)),
    h("p", { class: "muted" }, "Signature ", h("span", { class: "mono" }, p.signature), ` · class ${p.cls || "-"} · consistency ${p.consistency == null ? "-" : Math.round(p.consistency * 100) + "%"} · runs ${JSON.stringify(r)}`),
    p.t1_blocked ? h("div", { class: "callout route" }, `T1 blocked: ${p.t1_blocked}`) : null,
    h("div", { class: "detail-grid" },
      h("section", {}, h("h4", {}, "History"),
        (p.history || []).length
          ? h("ol", { class: "hist" }, p.history.map((x) => h("li", {},
              h("span", { class: "hist-ev " + (x.event === "demoted" ? "bad" : "") }, x.event),
              x.from || x.to ? h("span", { class: "mono" }, ` ${x.from || "-"} → ${x.to || "-"}`) : null,
              h("div", { class: "muted" }, `${x.ts ? ago(x.ts) : ""}${x.incident ? " · " + x.incident : ""}${x.reason ? " · " + x.reason : ""}`),
              x.incident ? h("button", { type: "button", class: "linkish", onclick: () => openTrace(NS_DEFAULT, x.incident) }, "open trace") : null)))
          : emptyBox("No history.")),
      h("section", {}, h("h4", {}, "Steps (replayed in order)"),
        h("ol", { class: "steps-list" }, (p.steps || []).map((s, i) => h("li", {},
          h("strong", { class: "mono" }, `${s.server}.${s.tool}`), h("pre", { class: "diff" }, JSON.stringify(s.args, null, 2)))))),
      h("section", {}, h("h4", {}, "Extracts (where each value comes from)"),
        (p.extracts || []).length
          ? h("ul", { class: "plain" }, p.extracts.map((x) => h("li", {}, h("code", {}, "${" + x.var + "}"), ` ← output of step ${x.from_step}`, h("pre", { class: "diff" }, JSON.stringify(x.rule))))) : emptyBox("No extracts: this playbook can never run at T1."),
        h("h4", {}, "Edit template"),
        h("p", { class: "muted mono" }, `file: ${p.file_template || "-"}`), tmpl)));
}
const NS_DEFAULT = "demo";

// ------------------------------------------------------------ tabs and wiring
const TABS = ["overview", "traces", "knowledge"];
function showTab(id) {
  for (const t of TABS) {
    $("view-" + t).hidden = t !== id;
    const b = $("tab-" + t); b.setAttribute("aria-selected", String(t === id)); b.classList.toggle("on", t === id);
  }
  try { localStorage.setItem("tab", id); } catch (e) { /* ignore */ }
  if (id === "traces") loadRecentFlows();
  if (id === "knowledge") loadKnowledge();
}
for (const t of TABS) $("tab-" + t).addEventListener("click", () => showTab(t));
$("trace-close").addEventListener("click", closeTrace);
$("trace").addEventListener("cancel", (e) => { e.preventDefault(); closeTrace(); });
$("trace").addEventListener("close", () => { clearInterval(T.timer); T.timer = null; });
$("trace-search").addEventListener("input", (e) => { T.q = e.target.value; paintTimeline(true); });
$("trace-expand").addEventListener("click", () => {
  const exp = T.events.filter((e) => !["guardrail", "route", "learn"].includes(e.kind));
  if (T.open.size) T.open.clear(); else exp.forEach((e) => T.open.add(e.id));
  paintTimeline(true);
});
setInterval(() => {
  if (document.hidden) return;
  if (!$("view-traces").hidden) loadRecentFlows();
  if (!$("view-knowledge").hidden) loadKnowledge();
}, 5000);
try { const saved = localStorage.getItem("tab"); if (TABS.includes(saved)) showTab(saved); } catch (e) { /* ignore */ }
