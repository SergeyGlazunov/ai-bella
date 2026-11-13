#!/bin/sh
# Reload bsl-indexer daemon (R03 / F03). Require reloaded=true. No SIGHUP.
set -eu
CONTAINER="${BSL_CONTAINER:-bsl-indexer}"
export CODE_INDEX_HOME="${CODE_INDEX_HOME:-/data/code-index-home}"
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "reload_bsl_watch: python3/python not found" >&2
  exit 1
fi

out="$(docker exec -e CODE_INDEX_HOME="$CODE_INDEX_HOME" "$CONTAINER" \
  bsl-indexer daemon reload 2>&1)" || {
  echo "reload_bsl_watch: daemon reload failed" >&2
  echo "$out" >&2
  exit 1
}
printf '%s\n' "$out"

printf '%s' "$out" | "$PYTHON" -c '
import json, sys
text = sys.stdin.read()
decoder = json.JSONDecoder()
fallback = None
for i, ch in enumerate(text):
    if ch != "{":
        continue
    try:
        data, _ = decoder.raw_decode(text, i)
    except json.JSONDecodeError:
        continue
    if not isinstance(data, dict):
        continue
    if "reloaded" in data:
        if data.get("reloaded") is True:
            sys.exit(0)
        sys.stderr.write("reload_bsl_watch: reloaded!=true error=%r\n" % (data.get("error"),))
        sys.exit(1)
    if fallback is None:
        fallback = data
if fallback is not None:
    sys.stderr.write("reload_bsl_watch: JSON without reloaded key: %r\n" % (fallback,))
    sys.exit(1)
sys.stderr.write("reload_bsl_watch: no JSON object in output\n")
sys.exit(1)
'
