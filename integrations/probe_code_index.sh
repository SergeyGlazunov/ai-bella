#!/bin/sh
# Content probe for BSL code-index (R02 / F02).
# Usage: probe_code_index.sh present|absent <rel-path> [more paths...]
# Multiple paths share ONE docker exec into the BSL container.
# Exit 0 confirmed; 1 wrong presence; 2 control / uncertain (never "absent success").
set -eu

MODE="${1:?mode present|absent}"
shift
[ "$#" -ge 1 ] || {
  echo "probe_code_index: need at least one path" >&2
  exit 2
}
CONTAINER="${BSL_CONTAINER:-bsl-indexer}"
SRC="${BSL_SRC_PATH:-/repos/SRC}"

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  PYTHON=
fi

# One exec: dump get-file-summary for each path with a clear separator.
set +e
out="$(docker exec -e CODE_INDEX_HOME=/data/code-index-home "$CONTAINER" \
  sh -c '
SRC="$1"; shift
for rel in "$@"; do
  rel=$(printf "%s" "$rel" | tr "\\" "/")
  echo "__PROBE_PATH__:$rel"
  bsl-indexer get-file-summary "$rel" --path "$SRC" 2>&1 || echo "__PROBE_RC__:$?"
done
' sh "$SRC" "$@")"
rc=$?
set -e

classify_chunk() {
  chunk="$1"
  found=0
  not_found=0
  if [ -n "$PYTHON" ]; then
    verdict=$(printf '%s' "$chunk" | "$PYTHON" -c '
import json, sys
text = sys.stdin.read()
stripped = text.strip()
if stripped == "" or stripped == "null":
    print("absent")
    raise SystemExit
decoder = json.JSONDecoder()
obj = None
for i, ch in enumerate(text):
    if ch != "{":
        continue
    try:
        obj, _ = decoder.raw_decode(text, i)
        break
    except json.JSONDecodeError:
        continue
if obj is None:
    low = text.lower()
    if "not found" in low or "no such" in low or "не найден" in low:
        print("absent")
    else:
        print("unknown")
    raise SystemExit
if isinstance(obj, dict):
    err = obj.get("error")
    if isinstance(err, str) and ("не найден" in err.lower() or "not found" in err.lower()):
        print("absent")
    elif (
        "file" in obj
        or "path" in obj
        or "file_path" in obj
        or "functions" in obj
        or "classes" in obj
        or "variables" in obj
        or obj.get("oversize") is True
    ):
        print("present")
    elif "error" in obj:
        print("unknown")
    else:
        print("present")
else:
    print("unknown")
' 2>/dev/null) || verdict=unknown
  else
    if printf '%s' "$chunk" | grep -Eq '"file"|"path"|file_path|"functions"|"classes"|"variables"|"oversize"[[:space:]]*:[[:space:]]*true'; then
      verdict=present
    elif printf '%s' "$chunk" | grep -Eiq 'not found|no such|File not found|не найден|^null$'; then
      verdict=absent
    else
      verdict=unknown
    fi
  fi
  case "$verdict" in
    present) found=1 ;;
    absent) not_found=1 ;;
  esac
  printf '%s %s\n' "$found" "$not_found"
}

fail=0
uncertain=0
checked=0
current_path=""
current_chunk=""

flush() {
  [ -n "$current_path" ] || return 0
  checked=$((checked + 1))
  set -- $(classify_chunk "$current_chunk")
  found=$1
  not_found=$2
  case "$MODE" in
    present)
      if [ "$found" -eq 1 ]; then
        :
      elif [ "$not_found" -eq 1 ]; then
        echo "probe_code_index: expected present: $current_path" >&2
        fail=1
      else
        echo "probe_code_index: uncertain present: $current_path" >&2
        uncertain=1
      fi
      ;;
    absent)
      if [ "$found" -eq 1 ]; then
        echo "probe_code_index: expected absent: $current_path" >&2
        fail=1
      elif [ "$not_found" -eq 1 ]; then
        :
      else
        echo "probe_code_index: uncertain absent: $current_path" >&2
        uncertain=1
      fi
      ;;
  esac
}

# Split docker output into per-path chunks.
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    __PROBE_PATH__:*)
      flush
      current_path=${line#__PROBE_PATH__:}
      current_chunk=""
      ;;
    *)
      current_chunk="${current_chunk}${line}
"
      ;;
  esac
done <<EOF
$out
EOF
flush

if [ "$rc" -eq 125 ] || [ "$rc" -eq 126 ] || [ "$rc" -eq 127 ]; then
  echo "probe_code_index: control failure rc=$rc" >&2
  echo "$out" >&2
  exit 2
fi
if [ "$checked" -eq 0 ]; then
  echo "probe_code_index: no paths checked" >&2
  echo "$out" >&2
  exit 2
fi
if [ "$fail" -ne 0 ]; then
  echo "$out" >&2
  exit 1
fi
if [ "$uncertain" -ne 0 ]; then
  exit 2
fi
exit 0
