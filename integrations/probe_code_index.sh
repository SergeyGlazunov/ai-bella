#!/bin/sh
# Content probe for BSL code-index (R02 / F02).
# Usage: probe_code_index.sh present|absent <rel-path>
# Exit 0 confirmed; 1 wrong presence; 2 control / uncertain (never "absent success").
set -eu

MODE="${1:?mode present|absent}"
REL="${2:?relative path}"
CONTAINER="${BSL_CONTAINER:-bsl-indexer}"
SRC="${BSL_SRC_PATH:-/repos/SRC}"

REL=$(printf '%s' "$REL" | tr '\\' '/')

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  PYTHON=
fi

set +e
out="$(docker exec -e CODE_INDEX_HOME=/data/code-index-home "$CONTAINER" \
  bsl-indexer get-file-summary "$REL" --path "$SRC" 2>&1)"
rc=$?
set -e

found=0
not_found=0

classify() {
  # Prefer structured JSON from CLI (Option[FileSummary] → object or null).
  if [ -n "$PYTHON" ]; then
    verdict=$(printf '%s' "$out" | "$PYTHON" -c '
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
    case "$verdict" in
      present) found=1 ;;
      absent) not_found=1 ;;
    esac
    return
  fi
  # Fallback without Python: accept empty-module summaries ("functions": []) and oversize.
  if printf '%s' "$out" | grep -Eq '"file"|"path"|file_path|"functions"|"classes"|"variables"|"oversize"[[:space:]]*:[[:space:]]*true'; then
    found=1
  elif printf '%s' "$out" | grep -Eiq 'not found|no such|File not found|не найден|^null$'; then
    not_found=1
  fi
}

classify

control_fail=0
case "$rc" in
  125|126|127) control_fail=1 ;;
esac
if [ "$rc" -ne 0 ] && [ "$found" -eq 0 ] && [ "$not_found" -eq 0 ]; then
  control_fail=1
fi

case "$MODE" in
  present)
    if [ "$control_fail" -eq 1 ]; then
      echo "probe_code_index: control failure rc=$rc" >&2
      echo "$out" >&2
      exit 2
    fi
    if [ "$found" -eq 1 ]; then
      exit 0
    fi
    echo "probe_code_index: expected present: $REL" >&2
    echo "$out" >&2
    exit 1
    ;;
  absent)
    if [ "$control_fail" -eq 1 ]; then
      echo "probe_code_index: control failure (not confirmed absent) rc=$rc" >&2
      echo "$out" >&2
      exit 2
    fi
    if [ "$found" -eq 1 ]; then
      echo "probe_code_index: expected absent: $REL" >&2
      echo "$out" >&2
      exit 1
    fi
    if [ "$not_found" -eq 1 ] || [ "$rc" -eq 0 ]; then
      # rc=0 with null / no file markers → empty/null summary
      exit 0
    fi
    echo "probe_code_index: cannot confirm absent: $REL" >&2
    echo "$out" >&2
    exit 2
    ;;
  *)
    echo "probe_code_index: bad mode $MODE" >&2
    exit 2
    ;;
esac
