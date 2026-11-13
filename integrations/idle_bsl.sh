#!/bin/sh
# Thin CLI shim → contour.bsl_ctl.idle_bsl (orchestrator calls Python directly).
set -eu
ROOT="${AI_CONTOUR_ROOT:-/opt/ai-contour}"
export PYTHONPATH="${ROOT}/controller/src${PYTHONPATH:+:$PYTHONPATH}"
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "idle_bsl: python3/python not found" >&2
  exit 1
fi
exec "$PYTHON" -c 'from contour.bsl_ctl import idle_bsl; import os, sys; sys.exit(0 if idle_bsl(os.environ.get("BSL_CONTAINER", "bsl-indexer")) else 1)'
