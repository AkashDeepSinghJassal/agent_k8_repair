"""Crystallization: agent trace -> playbook, extract rules, promotion and demotion (V4_CONTRACT.md sections 2 and 4)."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import flow
from .config import Settings, get_settings
from .playbooks import Extract, Playbook, PlaybookStore, Step, default_store, playbook_id, push_history

log = logging.getLogger(__name__)

CONSISTENCY_MIN = 0.9
_PLACEHOLDER = re.compile(r"\$\$\{|\$\{(\w+)\}")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_KV = re.compile(r"^(\s*(?:-\s+)?)([\w./\-]+)(\s*[:=]\s*)(.*?)(\s*)$")
_SEED_ORDER = ("file", "pod", "owner", "container", "deployment", "repo", "namespace", "app")
NON_REPLAY_TOOLS = frozenset({"propose_patch", "escalate"})
_FIRST_SHA = {"type": "regex", "pattern": r'"sha":\s*"([0-9a-f]{7,40})"', "group": 1}
_SYMPTOM_TOOLS = frozenset({"k8s_logs", "k8s_describe_pod"})


# --- variables and templates --------------------------------------------------------


def seed_vars(seed: dict) -> dict[str, str]:
    owner = seed.get("owner") or ""
    vals = {
        "namespace": seed.get("namespace"), "pod": seed.get("name"), "container": seed.get("container"),
        "owner": owner, "deployment": owner.split("/", 1)[1] if "/" in owner else "",
        "file": seed.get("path") or seed.get("file"), "repo": seed.get("repo"), "app": seed.get("app"),
    }
    return {k: str(v) for k, v in vals.items() if v}


def escape_literal(text: str) -> str:
    return text.replace("${", "$${")


def substitute(value: Any, ctx: dict[str, str]) -> Any:
    """Replace ``${var}`` in strings (recursively). Unknown variables raise ``KeyError``; ``$${`` is a literal ``${``."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            if m.group(1) is None:
                return "${"
            if m.group(1) not in ctx:
                raise KeyError(m.group(1))
            return ctx[m.group(1)]

        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {k: substitute(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, ctx) for v in value]
    return value


def blank_variables(text: str) -> str:
    """Template text with each variable shown as ``<FILL var>`` (for the T2 prompt)."""
    return _PLACEHOLDER.sub(lambda m: "${" if m.group(1) is None else f"<FILL {m.group(1)}>", text)


def _parameterize_args(args: dict, variables: dict[str, str]) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and v:
            name = k if variables.get(k) == v else next((n for n in _SEED_ORDER if variables.get(n) == v), None)
            out[k] = "${" + name + "}" if name else escape_literal(v)
        else:
            out[k] = v
    return out


# --- extract rules --------------------------------------------------------------------


def _parse_path(path: str) -> list[str | int]:
    toks: list[str | int] = []
    for part in re.finditer(r"([^.\[\]]+)|\[(\d+)\]", path):
        toks.append(part.group(1) if part.group(1) is not None else int(part.group(2)))
    return toks


def _json_walk(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _json_walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _json_walk(v, f"{prefix}[{i}]")
    else:
        out.append((prefix, obj))
    return out


def apply_rule(rule: dict, text: str) -> str | None:
    """Apply one extract rule to a tool output; None when it does not match."""
    try:
        kind = rule.get("type")
        if kind == "regex":
            m = re.search(rule["pattern"], text, re.MULTILINE)
            return m.group(int(rule.get("group", 1))) if m else None
        if kind == "json_path":
            node: Any = json.loads(text)
            for tok in _parse_path(rule["path"]):
                node = node[tok]
            return None if isinstance(node, dict | list) else str(node)
        if kind == "diff_removed_line":
            pat = re.compile(rf"^-\s*{re.escape(rule['key'])}\s*[:=]\s*(.*?)\s*$")
            for line in text.splitlines():
                if line.startswith("-") and not line.startswith("---") and (m := pat.match(line)):
                    return m.group(1).strip("\"'")
    except (ValueError, KeyError, IndexError, TypeError, re.error):
        return None
    return None


def _json_rule(text: str, value: str) -> dict | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    for path, leaf in _json_walk(obj):
        if str(leaf) == value and path:
            return {"type": "json_path", "path": path}
    return None


def _context_rule(text: str, value: str) -> dict | None:
    body = r"(\S+)" if not re.search(r"\s", value) else r"([^\n]+)"
    start = 0
    while (i := text.find(value, start)) != -1:
        line_start = text.rfind("\n", 0, i) + 1
        for n in (28, 18, 10, 5):
            prefix = text[max(line_start, i - n) : i]
            if len(prefix.strip()) < 3:
                continue
            rule = {"type": "regex", "pattern": re.escape(prefix) + body, "group": 1}
            if apply_rule(rule, text) == value:
                return rule
        start = i + 1
    return None


def _sha_entry_rule(text: str, value: str) -> dict | None:
    """Rule for a commit sha in a git_log listing (concatenated JSON objects, not one document): its position in the list.

    Never anchored on the commit message: the next incident's log has other commits.
    """
    shas = re.findall(r'"sha":\s*"([0-9a-f]{7,40})"', text)
    if not _SHA.match(value) or value not in shas:
        return None
    n = shas.index(value)
    entry = r'"sha":\s*"[0-9a-f]{7,40}"'
    pattern = (r'(?:' + entry + r'[\s\S]*?){' + str(n) + r'}' if n else "") + r'"sha":\s*"([0-9a-f]{7,40})"'
    rule = {"type": "regex", "pattern": pattern, "group": 1}
    return rule if apply_rule(rule, text) == value else None


def derive_rule(tool: str, text: str, value: str, key: str | None = None) -> dict | None:
    """Best validated rule that reproduces ``value`` from ``text``: diff_removed_line, json_path, anchored regex."""
    if key and tool == "git_diff":
        rule = {"type": "diff_removed_line", "key": key}
        if apply_rule(rule, text) == value:
            return rule
    if (rule := _json_rule(text, value)) and apply_rule(rule, text) == value:
        return rule
    if tool == "git_log" and (rule := _sha_entry_rule(text, value)):
        return rule
    if key:
        body = r"([^\n\"']+)" if re.search(r"\s", value) else r"([^\s\"']+)"
        rule = {"type": "regex", "pattern": rf"{re.escape(key)}\s*[:=]\s*[\"']?{body}", "group": 1}
        if apply_rule(rule, text) == value:
            return rule
    return _context_rule(text, value)


# --- trace handling -------------------------------------------------------------------


def _text(entry: dict) -> str:
    return str(entry.get("result") or entry.get("result_preview") or "")


def _server(tool: str) -> str:
    head = tool.split("_", 1)[0]
    return head if head in ("k8s", "git", "argo", "repair") else "workflow" if tool == "workflow_status" else head


def kept_calls(trace: list[dict]) -> list[dict]:
    """Successful, non-final, de-duplicated tool calls in order."""
    out, seen = [], set()
    for t in trace:
        tool = t.get("tool", "")
        if tool in NON_REPLAY_TOOLS or t.get("error") or _text(t).startswith('{"error"'):
            continue
        key = (tool, json.dumps(t.get("args") or {}, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _new_lines(find: str, replace: str) -> list[str]:
    have = Counter(find.splitlines())
    out = []
    for line in replace.splitlines():
        if have[line] > 0:
            have[line] -= 1
        else:
            out.append(line)
    return out


def _literal_of(line: str) -> tuple[str | None, str, str, str, str] | None:
    """(key, value, prefix, suffix, raw_value) of a new line, or None when it carries no value."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped == "---":
        return None
    m = _KV.match(line)
    if m and m.group(4):
        raw = m.group(4)
        return m.group(2), raw.strip("\"'"), m.group(1) + m.group(2) + m.group(3), m.group(5), raw
    if m:
        return None  # "key:" opens a mapping, no value
    return None, stripped, line[: len(line) - len(line.lstrip())], line[len(line.rstrip()) :], stripped


def _provenance(value: str, key: str | None, calls: list[dict], variables: dict[str, str]) -> tuple[str, Extract | None] | None:
    """("seed", None) | ("step", Extract) | None. Extract.var is filled in by the caller."""
    for name in _SEED_ORDER:
        if variables.get(name) == value:
            return name, None
    found: list[Extract] = []
    for i, c in enumerate(calls):
        text = _text(c)
        if value in text and (rule := derive_rule(c["tool"], text, value, key)):
            found.append(Extract("", i, rule))
    # a value a commit removed is best read from that diff's removed line
    found.sort(key=lambda e: e.rule["type"] != "diff_removed_line")
    if found:
        return "step", found[0]
    return None


def _edit_template(edit: dict, calls: list[dict], variables: dict[str, str], extracts: list[Extract], blocked: list[str]) -> dict:
    find, replace = edit["find"], edit["replace"]
    lines = replace.split("\n")
    new = _new_lines(find, replace)
    new_idx = set()
    pool = list(new)
    for i, line in enumerate(lines):
        if line in pool:
            pool.remove(line)
            new_idx.add(i)
    for i in sorted(new_idx):
        lit = _literal_of(lines[i])
        if lit is None:
            continue
        key, value, prefix, suffix, raw = lit
        prov = _provenance(value, key, calls, variables)
        if prov is None:
            blocked.append(f"value {value!r} has no provenance")
            lines[i] = escape_literal(lines[i])
            continue
        kind, ex = prov
        if kind != "step":
            var = kind
        else:
            assert ex is not None
            existing = next((e for e in extracts if e.from_step == ex.from_step and e.rule == ex.rule), None)
            if existing is None:
                base = re.sub(r"\W", "_", (key or "val").lower())
                ex.var = base if not any(e.var == base for e in extracts) else f"{base}_{len(extracts) + 1}"
                extracts.append(ex)
                existing = ex
            var = existing.var
        quote = raw[0] if raw[:1] in "\"'" and raw[-1:] == raw[:1] and len(raw) > 1 else ""
        lines[i] = escape_literal(prefix) + quote + "${" + var + "}" + quote + escape_literal(suffix)
    return {"find": escape_literal(find), "replace": "\n".join(lines)}


def _prop_fields(proposal: Any) -> tuple[str, list[dict], str, str]:
    if isinstance(proposal, dict):
        file, edits = proposal.get("file") or "", proposal.get("edits") or []
        cls = proposal.get("failure_class") or proposal.get("class") or "OTHER"
        root = proposal.get("root_cause", "")
    else:
        file, edits, cls, root = proposal.file or "", proposal.edits, proposal.cls, proposal.root_cause
    norm = [e if isinstance(e, dict) else {"find": e.find, "replace": e.replace} for e in edits]
    return file, norm, cls, root


def capture(trace: list[dict], proposal: Any, seed: dict, *, signature: str = "") -> Playbook | None:
    """Crystallize a healed T3 run. None when the run has nothing replayable (no steps or no edits)."""
    file, edits, cls, root = _prop_fields(proposal)
    variables = seed_vars(seed)
    calls = kept_calls(trace)
    if not calls or not edits or not file:
        return None
    file_t = "${file}" if variables.get("file") == file else escape_literal(file)
    extracts: list[Extract] = []  # from_step is an index into ``calls`` until the final remap
    blocked: list[str] = []
    edits_t = [_edit_template(e, calls, variables, extracts, blocked) for e in edits]
    # minimal evidence path: the symptom (logs/describe), the file and its history, plus whatever feeds an extract
    keep = {i for i, c in enumerate(calls) if c["tool"] in _SYMPTOM_TOOLS or (
        c["tool"] in ("git_read", "git_log") and (c.get("args") or {}).get("path") == file)}
    keep |= {e.from_step for e in extracts}
    # args that are commit shas coming from an earlier result are bound dynamically (the sha changes per incident)
    all_args: dict[int, dict] = {}
    for i in sorted(keep, reverse=True):
        args = all_args[i] = _parameterize_args(calls[i].get("args") or {}, variables)
        for k, v in list(args.items()):
            if isinstance(v, str) and _SHA.match(v):
                for j in range(i):
                    rule = derive_rule(calls[j]["tool"], _text(calls[j]), v)
                    if rule and calls[j]["tool"] == "git_log" and file:
                        # git_log filters by path ({sha, date, message} only): the introducing commit of a fresh incident is
                        # assumed to be the NEWEST commit touching the edited file, whatever index it had in the captured run
                        rule = {"type": "json_path", "path": "[0].sha"} if _text(calls[j]).lstrip().startswith("[") else dict(_FIRST_SHA)
                        all_args[j] = {**(all_args.get(j) or _parameterize_args(calls[j].get("args") or {}, variables)),
                                       "path": file_t}
                    if rule:
                        same = next((e for e in extracts if e.from_step == j and e.rule == rule), None)
                        var = same.var if same else f"sha_{j}" if not any(e.var == f"sha_{j}" for e in extracts) else f"sha_{j}_{len(extracts)}"
                        if same is None:
                            extracts.append(Extract(var, j, rule))
                        args[k] = "${" + var + "}"
                        keep.add(j)
                        break
                else:
                    args[k] = v
    for i in keep:
        all_args.setdefault(i, _parameterize_args(calls[i].get("args") or {}, variables))
    order = sorted(keep)
    pos = {i: n for n, i in enumerate(order)}
    for e in extracts:
        e.from_step = pos[e.from_step]
    steps = [Step(_server(calls[i]["tool"]), calls[i]["tool"], all_args[i]) for i in order]
    calls = [calls[i] for i in order]
    seq = [c["tool"] for c in calls]
    sig = signature or seed.get("signature", "")
    pb = Playbook(
        id=playbook_id(sig), signature=sig, cls=cls, state="T3", steps=steps, extracts=extracts,
        file_template=file_t, edits_template=edits_t, action_sequence=seq,
        meta={"seen": 1, "same": 1, "streak": 0, "root_cause": root},
    )
    if blocked:
        pb.meta["t1_blocked"] = "; ".join(dict.fromkeys(blocked))
    return pb


# --- rendering (shared by T1 and the T2 comparison) -------------------------------------


def render(pb: Playbook, ctx: dict[str, str]) -> tuple[str, list[dict]]:
    """File and edits with every variable bound; raises KeyError when one is missing."""
    return substitute(pb.file_template, ctx), [substitute(e, ctx) for e in pb.edits_template]


# --- events ---------------------------------------------------------------------------------


def learn_event(incident: str, namespace: str, action: str, pb: Playbook | None, reason: str, *,
                frm: str | None = None, to: str | None = None, tier: str | None = None) -> None:
    pid = pb.id if pb else None
    titles = {
        "captured": f"Captured playbook {pid} from this run",
        "observed": f"Playbook {pid} observed another verified {tier or 'T3'} run",
        "promoted": f"Promoted playbook {pid} {frm} -> {to}",
        "demoted": f"Demoted playbook {pid} {frm} -> {to}",
        "skipped": "Learning skipped",
        "collision": f"Playbook {pid} does not apply (signature collision)",
    }
    title = titles.get(action, action) + (f": {reason}" if action in ("skipped", "demoted") and reason else "")
    flow.emit(incident, namespace, "learn", title=title, tier=tier, status="skipped" if action == "skipped" else "ok", data={
        "action": action, "playbook_id": pid, "from_state": frm or (pb.state if pb else None),
        "to_state": to or (pb.state if pb else None), "counts": dict(pb.runs) if pb else {},
        "consistency": pb.consistency if pb else None, "reason": reason,
    })


# --- promotion / demotion ----------------------------------------------------------------------


def maybe_promote(pb: Playbook, store: PlaybookStore, incident: str = "", namespace: str = "",
                  settings: Settings | None = None, note: str = "") -> str | None:
    """Promote one level when the evidence is there. Returns the new state or None."""
    s = settings or get_settings()
    streak = pb.meta.get("streak", 0)
    new = reason = None
    if pb.state == "T3":
        if (pb.runs["t3_ok"] >= s.promote_t2_after and streak >= s.promote_t2_after and pb.consistency >= CONSISTENCY_MIN
                and pb.runs["edited"] == 0 and pb.runs["rejected"] == 0):
            new = "T2"
            reason = (f"{pb.runs['t3_ok']} verified agent runs, consistency {pb.consistency:.2f}, "
                      "no guardrail rejections or reviewer edits")
    elif pb.state == "T2" and not pb.t1_blocked and streak >= s.promote_t1_after:
        new = "T1"
        reason = f"{streak} verified hybrid runs where the LLM's edit equalled the extract rules' edit"
    if new is None:
        return None
    if note:
        reason = f"{reason}; {note}"
    frm = pb.state
    pb.state = new
    pb.meta["streak"] = 0
    push_history(pb, "promoted", frm, new, reason or "", incident)
    store.upsert(pb)
    learn_event(incident, namespace, "promoted", pb, reason or "", frm=frm, to=new)
    return new


def demote(pb: Playbook, store: PlaybookStore, reason: str, incident: str = "", namespace: str = "",
           *, bump_fail: bool = True) -> str | None:
    """Circuit breaker: one level down (T1 -> T2 -> T3). Returns the new state, or None when already T3."""
    if bump_fail:
        pb.runs["fail"] += 1
    pb.meta["streak"] = 0
    frm = pb.state
    if frm == "T3":
        push_history(pb, "failed", frm, frm, reason, incident)
        store.upsert(pb)
        learn_event(incident, namespace, "skipped", pb, f"candidate playbook failed: {reason}")
        return None
    new = "T2" if frm == "T1" else "T3"
    pb.state = new
    push_history(pb, "demoted", frm, new, reason, incident)
    store.upsert(pb)
    learn_event(incident, namespace, "demoted", pb, reason, frm=frm, to=new)
    return new


# --- the learning hook ---------------------------------------------------------------------------


def _load_trace(name: str, attempt: int, settings: Settings) -> dict | None:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    try:
        return json.loads((Path(settings.state_dir).expanduser() / "agent-traces" / f"{safe}-a{attempt}.json").read_text())
    except (OSError, ValueError):
        return None


def is_consistent(pb: Playbook, fresh: Playbook | None, trace: list[dict]) -> bool:
    """Essential-path consistency (V4_CONTRACT.md section 8): the run's trace contains the playbook's essential steps
    (multiset containment of its pruned ``action_sequence``, order and extra exploratory calls ignored) and the run's
    edit, templated the same way, equals the playbook's ``edits_template`` (and file)."""
    if fresh is None:
        return False
    have = Counter(c["tool"] for c in kept_calls(trace))
    if Counter(pb.action_sequence) - have:
        return False
    return fresh.edits_template == pb.edits_template and fresh.file_template == pb.file_template


def _record_sequence(pb: Playbook, fresh: Playbook | None, trace: list[dict] | None = None) -> None:
    pb.meta["seen"] = pb.meta.get("seen", 1) + 1
    if is_consistent(pb, fresh, trace or []):
        pb.meta["same"] = pb.meta.get("same", 1) + 1
    pb.consistency = round(pb.meta["same"] / pb.meta["seen"], 4)


def learn_from_incident(
    body: dict, patch: dict, repo: Any = None, *, store: PlaybookStore | None = None, settings: Settings | None = None,
    fetch: Callable[[Any, str, str], str] | None = None, edits_applied: Callable[[str, list[dict]], bool] | None = None,
) -> str | None:
    """Terminal-phase learner for hybrid mode. Returns the action taken (also emitted as a ``learn`` event)."""
    from .verify_handlers import edits_applied as default_applied
    from .verify_handlers import fetch_merged_file, merge_sha

    status = body.get("status") or {}
    if status.get("hybridLearning"):
        return None
    settings = settings or get_settings()
    store = store or default_store(settings)
    fetch = fetch or fetch_merged_file
    edits_applied = edits_applied or default_applied
    meta = body.get("metadata") or {}
    name, ns = meta.get("name", ""), meta.get("namespace", "")
    phase = status.get("phase")
    agent = status.get("agent") or {}
    tier, pb_id = agent.get("tierUsed"), agent.get("playbookId")
    sig = status.get("signature") or (meta.get("annotations") or {}).get("repair.example.dev/signature")
    prop = status.get("proposal") or {}
    sha = merge_sha(status)

    def done(action: str, reason: str) -> str:
        patch.setdefault("status", {})["hybridLearning"] = {"action": action, "reason": reason, "playbook": pb_id, "tier": tier}
        return action

    def skip(reason: str) -> str:
        learn_event(name, ns, "skipped", store.get(pb_id) if pb_id else None, reason, tier=tier)
        return done("skipped", reason)

    pb = store.get(pb_id) if pb_id else (store.get_by_signature(sig) if sig else None)

    if phase != "Healed":
        if tier in ("T1", "T2") and pb is not None and (sha or phase == "Failed"):
            if pb.state != tier:
                return skip(f"playbook {pb.id} was already demoted to {pb.state}")
            why = f"incident ended {phase}" + (f" ({(status.get('outcome') or {}).get('reason')})" if status.get("outcome") else "")
            demote(pb, store, f"{tier} run did not heal: {why}", name, ns)
            return done("demoted", why)
        return skip(f"incident ended {phase}; nothing verified to learn from")

    if not sig:
        return skip("incident has no signature")
    if not sha or not prop.get("edits") or not prop.get("file"):
        return skip("no merged proposal to compare")
    try:
        if repo is None:
            raise LookupError("no repository client")
        matches = edits_applied(fetch(repo, prop["file"], sha), prop["edits"])
    except Exception as exc:  # noqa: BLE001
        return skip(f"cannot compare the merged file with the proposal: {exc}")
    if not matches:
        if pb is not None:
            pb.runs["edited"] += 1
            if tier in ("T1", "T2") and pb.state == tier:
                demote(pb, store, "reviewer edited the fix", name, ns, bump_fail=False)
                return done("demoted", "reviewer edited the fix")
            pb.meta["streak"] = 0
            store.upsert(pb)
        return skip("reviewer edited the fix; the merged change differs from the proposal")
    attempts = status.get("attempts") or 1
    if attempts > 1 or status.get("reviewFeedback"):
        if pb is not None:
            pb.runs["rejected"] += 1
            store.upsert(pb)
        return skip(f"healed only after {attempts} attempts (guardrail or reviewer feedback); not a clean run")

    if tier == "T3" or tier is None:
        stored = _load_trace(name, attempts, settings)
        if not stored or not stored.get("trace"):
            return skip("no agent trace saved for this run")
        fresh = capture(stored["trace"], prop, stored.get("seed") or {}, signature=sig)
        if fresh is None:
            return skip("trace has no replayable steps")
        if pb is None:
            fresh.runs["t3_ok"] = 1
            fresh.meta["streak"] = 1
            push_history(fresh, "captured", "-", "T3", "healed T3 run", name)
            store.upsert(fresh)
            learn_event(name, ns, "captured", fresh, "Healed run with merged change equal to the proposal", tier="T3")
            maybe_promote(fresh, store, name, ns, settings)
            return done("captured", "new playbook")
        pb.runs["t3_ok"] += 1
        pb.meta["streak"] = pb.meta.get("streak", 0) + 1
        _record_sequence(pb, fresh, stored["trace"])
        push_history(pb, "observed", pb.state, pb.state, f"consistency {pb.consistency:.2f}", name)
        store.upsert(pb)
        learn_event(name, ns, "observed", pb, f"consistency {pb.consistency:.2f} over {pb.meta['seen']} runs", tier="T3")
        maybe_promote(pb, store, name, ns, settings)
        return done("observed", "T3 run counted")

    if pb is None:
        return skip(f"{tier} run but playbook {pb_id} no longer exists")
    key = "t2" if tier == "T2" else "t1"
    pb.runs[f"{key}_ok"] += 1
    if tier == "T2":
        if agent.get("rulesMatch"):
            pb.runs["t2_match"] += 1
            pb.meta["streak"] = pb.meta.get("streak", 0) + 1
        else:
            pb.meta["streak"] = 0
    push_history(pb, f"{key}_ok", pb.state, pb.state, "verified heal", name)
    store.upsert(pb)
    learn_event(name, ns, "observed", pb, f"verified {tier} heal" + (
        "; LLM edit equals extract-rule edit" if tier == "T2" and agent.get("rulesMatch") else ""), tier=tier)
    maybe_promote(pb, store, name, ns, settings)
    return done("observed", f"{tier} run counted")
