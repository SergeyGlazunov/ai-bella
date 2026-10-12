#!/bin/sh
# Fail-closed idle probe for bsl-indexer (R03 / F04).
set -eu
CONTAINER="${BSL_CONTAINER:-bsl-indexer}"

if ! status="$(docker exec "$CONTAINER" bsl-indexer daemon status --json 2>/dev/null)"; then
  echo "idle_bsl: cannot reach daemon status" >&2
  exit 1
fi

printf '%s' "$status" | python -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.stderr.write("idle_bsl: invalid JSON\n")
    sys.exit(1)
if not isinstance(data, dict) or data.get("status") != "running":
    sys.exit(1)
paths = data.get("paths")
if not isinstance(paths, list):
    sys.exit(1)
busy = {"indexing", "reindexing", "initial_indexing", "stopping_workers", "error"}
for p in paths:
    if not isinstance(p, dict):
        sys.exit(1)
    st = p.get("status")
    if st in busy:
        sys.exit(1)
    if st is not None and st != "ready":
        sys.exit(1)
sys.exit(0)
' || {
  echo "idle_bsl: not idle or bad status JSON" >&2
  exit 1
}
