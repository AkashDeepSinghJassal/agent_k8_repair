#!/usr/bin/env bash
# Stop the operator and dashboard started by start_demo.sh (kills by PID file). Leaves the cluster running.
set -euo pipefail

RUN_DIR="${TMPDIR:-/tmp}/repair-loop"

for name in operator dashboard; do
  pidfile="$RUN_DIR/$name.pid"
  if [ -f "$pidfile" ]; then
    pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      # uv may spawn the real server as a child; stop children first.
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
      echo "stopped $name (pid $pid)"
    else
      echo "$name not running"
    fi
    rm -f "$pidfile"
  else
    echo "$name: no pid file"
  fi
done

# Also stop demo processes that are not in the pid files (stale runs from earlier sessions).
# Same command-line patterns as start_demo.sh.
DEMO_PATTERNS=("kopf run.*repair_operator" "uvicorn repair_dashboard")
pids=""
for pat in "${DEMO_PATTERNS[@]}"; do
  pids="$pids $(pgrep -f "$pat" 2>/dev/null || true)"
done
pids="$(echo $pids | tr ' ' '\n' | sort -un | tr '\n' ' ')"
if [ -n "${pids// /}" ]; then
  echo "stopping other demo processes:"
  for p in $pids; do ps -o pid=,command= -p "$p" 2>/dev/null || true; done
  for p in $pids; do kill "$p" 2>/dev/null || true; done
  alive=""
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
fi
