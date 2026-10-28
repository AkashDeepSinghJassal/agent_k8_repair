#!/usr/bin/env bash
# Start the repair-loop demo: cluster (if missing), CRD, operator and dashboard in the background.
# Secrets are read from the environment only; they are never printed, stored or logged.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROTO="$(cd "$HERE/.." && pwd)"
RUN_DIR="${TMPDIR:-/tmp}/repair-loop"
CONTEXT="kind-repair"

die() { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

for t in docker kubectl kind helm uv make; do need "$t"; done
docker info >/dev/null 2>&1 || die "docker is not running"

mkdir -p "$RUN_DIR"
cd "$PROTO"

if ! kind get clusters 2>/dev/null | grep -qx repair; then
  echo "==> kind cluster 'repair' missing, running make cluster-up"
  make cluster-up
fi
kubectl config use-context "$CONTEXT" >/dev/null || die "cannot switch to kube context $CONTEXT"
kubectl cluster-info >/dev/null 2>&1 || die "cluster $CONTEXT is not reachable (is the kind node running?)"

echo "==> applying CRD"
kubectl apply -f crd/repairincident.yaml >/dev/null

# GitHub token: env first, `gh auth token` only on explicit opt-in. Never printed.
if [ -z "${REPAIR_GITHUB_TOKEN:-}" ]; then
  if [ "${REPAIR_USE_GH_TOKEN:-0}" = "1" ]; then
    need gh
    REPAIR_GITHUB_TOKEN="$(gh auth token)" || die "gh auth token failed"
    export REPAIR_GITHUB_TOKEN
  else
    die "REPAIR_GITHUB_TOKEN is not set (export it, or set REPAIR_USE_GH_TOKEN=1 to use 'gh auth token')"
  fi
fi

# LLM provider: explicit, else groq if its key is set, else anthropic.
if [ -z "${REPAIR_LLM_PROVIDER:-}" ]; then
  if [ -n "${GROQ_API_KEY:-}" ]; then REPAIR_LLM_PROVIDER=groq; else REPAIR_LLM_PROVIDER=anthropic; fi
fi
export REPAIR_LLM_PROVIDER
case "${REPAIR_ANALYZER_MODE:-agent}:$REPAIR_LLM_PROVIDER" in
  agent:*) : ;; # agent mode: keys are checked by the agent host (groq key or the claude CLI), not here
  *:groq) [ -n "${GROQ_API_KEY:-}" ] || die "REPAIR_LLM_PROVIDER=groq but GROQ_API_KEY is not set" ;;
  *:anthropic) [ -n "${ANTHROPIC_API_KEY:-}" ] || die "REPAIR_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set" ;;
  *) die "unknown REPAIR_LLM_PROVIDER '$REPAIR_LLM_PROVIDER' (use groq or anthropic)" ;;
esac

# Analyzer: this branch (prototype-v3) defaults to the agent-over-MCP mode. Set REPAIR_ANALYZER_MODE=llm-first
# to use the single-shot LLM instead. REPAIR_AGENT_PROVIDER (groq | claude-cli) is passed through; the
# dashboard switch (agent-config.json) overrides it at runtime.
export REPAIR_ANALYZER_MODE="${REPAIR_ANALYZER_MODE:-agent}"
if [ -n "${REPAIR_AGENT_PROVIDER:-}" ]; then
  export REPAIR_AGENT_PROVIDER
  AGENT_PROVIDER_DESC="$REPAIR_AGENT_PROVIDER"
elif [ -n "${GROQ_API_KEY:-}" ]; then AGENT_PROVIDER_DESC="groq (auto)"
else AGENT_PROVIDER_DESC="claude-cli (auto)"; fi

# Demo timings: everything at 5 s (override by exporting these before running). The operator's own defaults
# are slower (debounce 60 s, PR poll 30 s, verify window 120 s, cooldown 600 s). A 5 s verify window only
# proves the pods are Ready and stable for 5 s; use a longer window for a real evaluation.
export REPAIR_DEBOUNCE_SECONDS="${REPAIR_DEBOUNCE_SECONDS:-5}"
export REPAIR_PR_POLL_SECONDS="${REPAIR_PR_POLL_SECONDS:-5}"
export REPAIR_VERIFY_WINDOW_SECONDS="${REPAIR_VERIFY_WINDOW_SECONDS:-5}"
export REPAIR_COOLDOWN_SECONDS="${REPAIR_COOLDOWN_SECONDS:-5}"

# Command-line patterns of the demo processes (matches the start commands below). Catches stale runs
# from earlier sessions that are not recorded in the pid files.
DEMO_PATTERNS=("kopf run.*repair_operator" "uvicorn repair_dashboard")
DASHBOARD_PORT=8090

stop_stale_procs() {
  local pids="" pat p alive i
  for pat in "${DEMO_PATTERNS[@]}"; do
    pids="$pids $(pgrep -f "$pat" 2>/dev/null || true)"
  done
  pids="$(echo $pids | tr ' ' '\n' | sort -un | tr '\n' ' ')"
  [ -n "${pids// /}" ] || return 0
  echo "found existing demo processes, stopping them:"
  for p in $pids; do ps -o pid=,command= -p "$p" 2>/dev/null || true; done
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  for i in 1 2 3 4 5 6 7 8 9 10; do
    alive=""
    for p in $pids; do
      if kill -0 "$p" 2>/dev/null; then alive="$alive $p"; fi
    done
    [ -n "$alive" ] || break
    sleep 1
  done
  if [ -n "$alive" ]; then
    echo "still running after 10 s, sending KILL:$alive"
    for p in $alive; do kill -KILL "$p" 2>/dev/null || true; done
  fi
}

check_port_free() {
  local holder
  holder="$(lsof -ti "tcp:$DASHBOARD_PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "${holder// /}" ]; then
    die "port $DASHBOARD_PORT is held by a process that is not part of the demo (pid(s):$holder). Stop it and rerun."
  fi
}

start() { # name, command...
  local name="$1"
  shift
  local pidfile="$RUN_DIR/$name.pid"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  nohup "$@" >"$RUN_DIR/$name.log" 2>&1 &
  echo $! >"$pidfile"
  echo "started $name (pid $!), log: $RUN_DIR/$name.log"
}

stop_stale_procs
check_port_free

start operator uv run kopf run -m repair_operator.main --all-namespaces --verbose
start dashboard uv run uvicorn repair_dashboard.app:app --port 8090

echo
echo "Analyzer mode: $REPAIR_ANALYZER_MODE"
[ "$REPAIR_ANALYZER_MODE" = agent ] && echo "Agent provider: $AGENT_PROVIDER_DESC (switch in the dashboard header)"
echo "LLM provider:  $REPAIR_LLM_PROVIDER (single-shot analyzer / fallback)"
echo "Dashboard:     http://localhost:8090"
echo "Demo app:      http://localhost:8080"
echo "Logs:          $RUN_DIR/"
echo "Stop with:     make demo-down"
