"""Bootstrap: raise Neo4j HTTP timeout / shrink delete batches before FULL index.

mcp_metadata_indexer constructs Neo4j(..., timeout=60) and ignores NEO4J_TIMEOUT_SEC.
On large УБД graphs DETACH DELETE batches exceed 60s → TimeoutError during
clear_metadata_layer. apply_changes.py already reads NEO4J_TIMEOUT_SEC.

Mounted at runtime as /app/neo4j_timeout_bootstrap.py for docker-run FULL.
"""
from __future__ import annotations

import os
import runpy
import sys

import graph_writer as gw

timeout = float(os.environ.get("NEO4J_TIMEOUT_SEC", "1800"))
batch = int(os.environ.get("NEO4J_DELETE_BATCH", "2000"))

_orig_init = gw.Neo4j.__init__


def _init(self, url, user, password, timeout_arg=60.0):
    _orig_init(self, url, user, password, timeout)


gw.Neo4j.__init__ = _init
gw.DELETE_BATCH_DEFAULT = batch

print(
    f"[bootstrap] NEO4J_TIMEOUT_SEC={timeout} NEO4J_DELETE_BATCH={batch}",
    flush=True,
)
sys.argv = ["/app/mcp_metadata_indexer.py", *sys.argv[1:]]
runpy.run_path("/app/mcp_metadata_indexer.py", run_name="__main__")
