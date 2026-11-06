"""T2: replay the tool calls, then one narrow LLM call without tools fills the edit."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import crystallize
from ..playbooks import Playbook
from . import TierError, TierResult, Tracer, check_already_fixed, read_files, replay

MAX_OUTPUT = 8000
LLM_TIMEOUT = 240


@dataclass
class LlmReply:
    text: str
    meta: dict = field(default_factory=dict)  # provider, model, input_tokens, output_tokens, cost_usd


LlmFn = Callable[[str], LlmReply]


def build_prompt(pb: Playbook, seed: dict, rep: Any, feedback: list[str] | None) -> str:
    parts = [
        ("You complete a known repair playbook for a Kubernetes GitOps incident. You have no tools. "
         "Use only the tool outputs below; every new value you write must appear verbatim in them. "
         "If the outputs show a recent commit that changed a value, restore that value rather than editing dependent resources to match it."),
        (f"Incident: {seed.get('reason', '')} on {seed.get('kind', '')} {seed.get('name', '')} in namespace {seed.get('namespace', '')}; "
         f"container {seed.get('container', '')}. {seed.get('message', '')}"),
        "## Tool outputs",
    ]
    for i, (step, res) in enumerate(zip(pb.steps, rep.steps, strict=False)):
        text = res["result"]
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "\n...[truncated]"
        parts.append(f"### step {i + 1}: {step.tool} {json.dumps(res['args'], default=str)}\n{text}")
    parts.append("## Edit template (values marked <FILL ...> are what you must determine)")
    parts.append(f"file: {crystallize.blank_variables(pb.file_template)}")
    parts.append(json.dumps([{k: crystallize.blank_variables(v) for k, v in e.items()} for e in pb.edits_template], indent=2))
    if feedback:
        parts.append("## Reviewer or guardrail feedback on the previous attempt\n" + "\n".join(f"- {f}" for f in feedback))
    parts.append(
        'Answer with ONE JSON object and nothing else: {"file": str, "edits": [{"find": str, "replace": str}], '
        '"root_cause": str, "confidence": number between 0 and 1}. Each "find" must match the current file exactly once.'
    )
    return "\n\n".join(parts)


def parse_reply(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise TierError("LLM reply contains no JSON object")
    try:
        obj = json.loads(m.group(0))
    except ValueError as exc:
        raise TierError(f"LLM reply is not valid JSON: {exc}") from exc
    edits = obj.get("edits")
    if not isinstance(obj.get("file"), str) or not isinstance(edits, list) or not edits or not all(
        isinstance(e, dict) and isinstance(e.get("find"), str) and isinstance(e.get("replace"), str) and e["find"] for e in edits
    ):
        raise TierError("LLM reply does not match the schema {file, edits[{find, replace}]}")
    return obj


def make_llm(provider: str | None = None) -> LlmFn:
    """Claude CLI (``claude -p --tools ""``) or Groq, chosen like the agent provider; falls back to the other once."""
    from repair_agent.config import fallback_enabled, other_provider, select_provider

    first = select_provider(provider)

    def call(prompt: str) -> LlmReply:
        try:
            return _CALLS[first](prompt)
        except Exception:
            if not fallback_enabled():
                raise
            return _CALLS[other_provider(first)](prompt)

    return call


def _claude(prompt: str) -> LlmReply:
    from repair_agent.claude_host import child_env, find_claude, kill_group
    from repair_agent.config import model_for

    claude = find_claude()
    if claude is None:
        raise TierError("claude CLI not found")
    model = model_for("claude-cli")
    with tempfile.TemporaryDirectory(prefix="repair-t2-") as cwd:
        popen = subprocess.Popen(
            [claude, "-p", "--tools", "", "--output-format", "json", "--model", model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, env=child_env(),
            start_new_session=True,
        )
        try:
            out, errtxt = popen.communicate(prompt, timeout=LLM_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            kill_group(popen)
            try:
                popen.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise TierError(f"claude timed out after {LLM_TIMEOUT}s") from exc
        finally:
            kill_group(popen)
    proc = subprocess.CompletedProcess(popen.args, popen.returncode, out, errtxt)
    if proc.returncode != 0:
        raise TierError(f"claude exited {proc.returncode}: {proc.stderr[-300:]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise TierError("claude printed no JSON") from exc
    if data.get("is_error"):
        raise TierError(f"claude error: {str(data.get('result'))[:300]}")
    u = data.get("usage") or {}
    meta = {"provider": "claude-cli", "model": model,
            "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
            "output_tokens": u.get("output_tokens") or 0}
    if data.get("total_cost_usd") is not None:
        meta["cost_usd"] = data["total_cost_usd"]
    return LlmReply(str(data.get("result") or ""), meta)


def _groq(prompt: str) -> LlmReply:
    import groq

    from repair_agent.config import model_for

    if not os.environ.get("GROQ_API_KEY"):
        raise TierError("GROQ_API_KEY not set")
    model = model_for("groq")
    resp = groq.Groq(max_retries=0, timeout=min(120, LLM_TIMEOUT)).chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], temperature=0)
    u = getattr(resp, "usage", None)
    return LlmReply(resp.choices[0].message.content or "", {
        "provider": "groq", "model": model,
        "input_tokens": getattr(u, "prompt_tokens", 0), "output_tokens": getattr(u, "completion_tokens", 0)})


_CALLS: dict[str, LlmFn] = {"claude-cli": _claude, "groq": _groq}


async def run(playbook: Playbook, seed: dict, mcp: Any, llm: LlmFn | None = None, tracer: Tracer | None = None,
              feedback: list[str] | None = None) -> TierResult:
    tracer = tracer or Tracer()
    rep = await replay(playbook, seed, mcp, tracer, strict=False)
    if rep.tool_failures and rep.tool_failures >= max(1, len(playbook.steps) // 2):
        raise TierError("too many replay steps failed: " + "; ".join(rep.errors[:3]))
    prompt = build_prompt(playbook, seed, rep, feedback)
    llm = llm or make_llm()
    t0 = time.monotonic()
    try:
        reply = llm(prompt)
    except Exception as exc:
        tracer.emit("llm_call", f"Narrow LLM call failed: {exc}", {"prompt": prompt, "error": str(exc)[:300]}, "error")
        raise TierError(f"LLM call failed: {exc}") from exc
    ms = int((time.monotonic() - t0) * 1000)
    tracer.emit("llm_call", f"One narrow LLM call (no tools) filled the edit for playbook {playbook.id}", {
        "provider": reply.meta.get("provider"), "model": reply.meta.get("model"), "prompt": prompt, "response": reply.text,
        "tokens": {"input": reply.meta.get("input_tokens"), "output": reply.meta.get("output_tokens")},
        "cost_usd": reply.meta.get("cost_usd"), "ms": ms}, duration_ms=ms)
    obj = parse_reply(reply.text)
    file, edits = obj["file"], [{"find": e["find"], "replace": e["replace"]} for e in obj["edits"]]
    text = rep.file_text(file)
    check_already_fixed(text, edits, file)
    if text is not None:
        for e in edits:
            if text.count(e["find"]) != 1:
                raise TierError(f"LLM edit anchor {e['find'][:60]!r} is not found exactly once in {file}")
    rules_match = False
    if not playbook.t1_blocked:
        try:
            rfile, redits = crystallize.render(playbook, rep.ctx)
            rules_match = rfile == file and redits == edits
        except KeyError:
            rules_match = False
    try:
        conf = float(obj.get("confidence", 0.8))
    except (TypeError, ValueError):
        conf = 0.8
    return TierResult(
        "T2", True, file=file, edits=edits, root_cause=str(obj.get("root_cause") or playbook.meta.get("root_cause", "")),
        confidence=max(0.0, min(1.0, conf)),
        rationale=f"Playbook {playbook.id}: replayed {len(playbook.steps)} tool calls, then one narrow LLM call filled the edit.",
        evidence="\n".join(rep.outputs), files=read_files(rep),
        trace=[{"step": i + 1, "tool": s["tool"], "args": s["args"], "result_preview": s["result"][:500]} for i, s in enumerate(rep.steps)],
        meta={**{k: v for k, v in reply.meta.items() if k in ("provider", "model", "input_tokens", "output_tokens", "cost_usd")},
              "steps": len(rep.steps), "rules_match": rules_match, "latency_ms": ms},
    )
