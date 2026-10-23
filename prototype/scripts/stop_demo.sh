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
