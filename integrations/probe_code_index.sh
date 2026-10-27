#!/bin/sh
# Content probe for BSL code-index (R02 / F02).
# Usage: probe_code_index.sh present|absent <rel-path>
# Exit 0 confirmed; 1 wrong presence; 2 control / uncertain (never "absent success").
#
# 2026-10-28: queries a THROWAWAY container mounting the daemon's /repos/SRC
# bind source. The daemon container's bind pins the inode from its start;
# after activation rotates the published dir, exec-ing the daemon would
# probe the pre-rotation tree (added files invisible → false verify
# failures). A fresh container resolves the current directory entry.
set -eu

MODE="${1:?mode present|absent}"
REL="${2:?relative path}"
CONTAINER="${BSL_CONTAINER:-bsl-indexer}"
SRC="${BSL_SRC_PATH:-/repos/SRC}"

REL=$(printf '%s' "$REL" | tr '\\' '/')

set +e
IMG=$(docker inspect "$CONTAINER" --format '{{.Image}}' 2>/dev/null)
SRCBIND=$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/repos/SRC"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
set -e
if [ -z "$IMG" ] || [ -z "$SRCBIND" ]; then
  echo "probe_code_index: cannot resolve bsl image/bind for $CONTAINER" >&2
  exit 2
fi

set +e
out="$(docker run --rm --network none \
  -e CODE_INDEX_HOME=/tmp/probe-cih \
  -v "$SRCBIND:/repos/SRC:ro" \
  "$IMG" bsl-indexer get-file-summary "$REL" --path "$SRC" 2>&1)"
rc=$?
set -e

found=0
not_found=0
# Prefer structured hit markers. Do not scan whole bodies for "не найден" —
# BSL source text often contains that phrase and would false-negative present.
if printf '%s' "$out" | grep -Eq '"path"|file_path|"functions"'; then
  found=1
fi
if [ "$found" -eq 0 ]; then
  if printf '%s' "$out" | grep -Eiq 'not found|no such|File not found|не найден|^null$'; then
    not_found=1
  fi
fi
# Bare null summary with no markers
if [ "$found" -eq 0 ] && [ "$not_found" -eq 0 ]; then
  if printf '%s' "$out" | grep -Eq '^null$'; then
    not_found=1
  fi
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
