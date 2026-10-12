#!/bin/sh
# Reload bsl-indexer daemon (R03 / F03). Require reloaded=true. No SIGHUP.
set -eu
CONTAINER="${BSL_CONTAINER:-bsl-indexer}"
export CODE_INDEX_HOME="${CODE_INDEX_HOME:-/data/code-index-home}"

out="$(docker exec -e CODE_INDEX_HOME="$CODE_INDEX_HOME" "$CONTAINER" \
  bsl-indexer daemon reload 2>&1)" || {
  echo "reload_bsl_watch: daemon reload failed" >&2
  echo "$out" >&2
  exit 1
}
printf '%s\n' "$out"

printf '%s' "$out" | python -c '
import json, sys
text = sys.stdin.read()
start, end = text.find("{"), text.rfind("}")
if start < 0 or end <= start:
    sys.stderr.write("reload_bsl_watch: no JSON in output\n")
    sys.exit(1)
data = json.loads(text[start : end + 1])
if data.get("reloaded") is True:
    sys.exit(0)
sys.stderr.write("reload_bsl_watch: reloaded!=true error=%r\n" % (data.get("error"),))
sys.exit(1)
'
