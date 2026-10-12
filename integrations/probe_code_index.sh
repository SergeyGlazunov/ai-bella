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

set +e
out="$(docker exec -e CODE_INDEX_HOME=/data/code-index-home "$CONTAINER" \
  bsl-indexer get-file-summary "$REL" --path "$SRC" 2>&1)"
rc=$?
set -e

found=0
not_found=0
if printf '%s' "$out" | grep -Eq '"path"|file_path|"functions"'; then
  found=1
fi
if printf '%s' "$out" | grep -Eiq 'not found|no such|File not found|не найден|^null$'; then
  not_found=1
  found=0
fi

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
      # rc=0 with no file markers and no error → empty/null summary
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
