from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Samples:
    bsl_paths: list[str]
    xml_paths: list[str]
    deletions: list[str]
    integrity_unknown: bool = False

    def is_empty(self) -> bool:
        return not self.bsl_paths and not self.xml_paths and not self.deletions


@runtime_checkable
class GitDiffRunner(Protocol):
    def diff_name_only(self, repo: str | Path, old_sha: str, new_sha: str) -> list[str]: ...

    def diff_deletions(self, repo: str | Path, old_sha: str, new_sha: str) -> list[str]: ...


class SubprocessGitDiffRunner:
    def diff_name_only(self, repo: str | Path, old_sha: str, new_sha: str) -> list[str]:
        result = subprocess.run(
            ["git", "diff", "--name-only", old_sha, new_sha],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def diff_deletions(self, repo: str | Path, old_sha: str, new_sha: str) -> list[str]:
        result = subprocess.run(
            ["git", "diff", "--diff-filter=D", "--name-only", old_sha, new_sha],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.splitlines() if line]


@runtime_checkable
class IndexProbe(Protocol):
    status: str

    def check_bsl(self, path: str) -> None: ...

    def check_xml(self, path: str) -> None: ...

    def check_deletion(self, path: str) -> None: ...


@runtime_checkable
class IndexProbes(Protocol):
    code_index: IndexProbe
    graph: IndexProbe


def normalize_index_path(path: str, workspace_subdir: str = "") -> str:
    """Strip repo workspace_subdir so probes see paths under /repos/SRC."""
    p = path.replace("\\", "/").lstrip("./")
    sub = (workspace_subdir or "").replace("\\", "/").strip().strip("/")
    if not sub:
        return p
    if p == sub:
        return ""
    prefix = sub + "/"
    if p.startswith(prefix):
        return p[len(prefix) :]
    return p


def sample_paths(
    repo: str | Path,
    old_sha: str,
    new_sha: str,
    *,
    git: GitDiffRunner | None = None,
    workspace_subdir: str = "",
) -> Samples:
    runner = git or SubprocessGitDiffRunner()
    all_paths = runner.diff_name_only(repo, old_sha, new_sha)
    deletion_set = set(runner.diff_deletions(repo, old_sha, new_sha))

    bsl_paths: list[str] = []
    xml_paths: list[str] = []
    for path in all_paths:
        if path in deletion_set:
            continue
        norm = normalize_index_path(path, workspace_subdir)
        if not norm:
            continue
        if norm.endswith(".bsl"):
            bsl_paths.append(norm)
        elif norm.endswith(".xml"):
            xml_paths.append(norm)

    deletions = sorted(
        {
            normalize_index_path(p, workspace_subdir)
            for p in deletion_set
            if normalize_index_path(p, workspace_subdir)
        }
    )

    return Samples(
        bsl_paths=bsl_paths,
        xml_paths=xml_paths,
        deletions=deletions,
    )


def sample_current_tree(
    repo: str | Path,
    *,
    workspace_subdir: str = "",
    limit: int = 32,
    required_layers: Sequence[str] | None = None,
    bsl_limit: int | None = None,
    xml_limit: int | None = None,
) -> Samples:
    """Recovery samples from working tree under workspace_subdir (capped).

    Limits for BSL and XML are independent so XML-heavy trees cannot starve the
    BSL probe required by ``code_index``. Unused layer types get a zero quota
    (e.g. klon_opl does not spend budget on XML).
    """
    layers = (
        frozenset(required_layers)
        if required_layers is not None
        else frozenset({"code_index", "graph"})
    )
    want_bsl = "code_index" in layers or "graph" in layers
    want_xml = "graph" in layers
    if bsl_limit is not None:
        max_bsl = bsl_limit
    elif want_bsl and want_xml:
        max_bsl = max(1, limit // 2)
    elif want_bsl:
        max_bsl = limit
    else:
        max_bsl = 0
    if xml_limit is not None:
        max_xml = xml_limit
    elif want_bsl and want_xml:
        max_xml = max(1, limit // 2)
    elif want_xml:
        max_xml = limit
    else:
        max_xml = 0

    root = Path(repo)
    sub = (workspace_subdir or "").replace("\\", "/").strip().strip("/")
    walk = root / sub if sub else root
    bsl_paths: list[str] = []
    xml_paths: list[str] = []
    if walk.is_dir():
        for path in sorted(walk.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(walk)
            if ".git" in rel.parts:
                continue
            posix = rel.as_posix()
            if posix.endswith(".bsl") and len(bsl_paths) < max_bsl:
                bsl_paths.append(posix)
            elif posix.endswith(".xml") and len(xml_paths) < max_xml:
                xml_paths.append(posix)
            bsl_done = len(bsl_paths) >= max_bsl
            xml_done = len(xml_paths) >= max_xml
            if bsl_done and xml_done:
                break
    return Samples(
        bsl_paths=bsl_paths,
        xml_paths=xml_paths,
        deletions=[],
        integrity_unknown=True,
    )


def verify_layers_at_revision(
    conn: sqlite3.Connection,
    database: str,
    revision: str,
    required_layers: Sequence[str],
) -> None:
    """Refuse publish when a required layer is missing or not at ``revision``."""
    rows = {
        str(row["layer"]): row
        for row in conn.execute(
            """
            SELECT layer, status, applied_revision FROM layer_state
            WHERE database = ?
            """,
            (database,),
        )
    }
    for layer in required_layers:
        row = rows.get(layer)
        if row is None:
            raise RuntimeError(f"required layer missing before publish: {layer}")
        if str(row["status"] or "") != "ready":
            raise RuntimeError(
                f"required layer not ready before publish: {layer} "
                f"(status={row['status']!r})"
            )
        if str(row["applied_revision"] or "") != revision:
            raise RuntimeError(
                f"required layer revision mismatch for {layer}: "
                f"{row['applied_revision']!r} != {revision!r}"
            )


def verify_run_layer_results(
    results: Sequence[object],
    required_layers: Sequence[str],
    revision: str,
) -> None:
    """Gate publish on this run's layer results, ignoring stale SQLite rows."""
    by_layer = {str(getattr(item, "layer")): item for item in results}
    for layer in required_layers:
        item = by_layer.get(layer)
        if item is None:
            raise RuntimeError(
                f"required layer missing from this run: {layer}"
            )
        item_rev = str(getattr(item, "revision", "") or "")
        if item_rev != revision:
            raise RuntimeError(
                f"required layer revision mismatch for {layer}: "
                f"{item_rev!r} != {revision!r}"
            )


def _verify_empty_layer_status(
    probes: IndexProbes, *, need_code: bool, need_graph: bool
) -> None:
    """Explicit empty-index contract: status must be ready, probes are not skipped."""
    if need_code and probes.code_index.status != "ready":
        raise RuntimeError(
            f"code_index probe not ready ({probes.code_index.status})"
        )
    if need_graph:
        graph = getattr(probes, "graph", None)
        if graph is None:
            raise RuntimeError("graph probe required for this database")
        if graph.status != "ready":
            raise RuntimeError(f"graph probe not ready ({graph.status})")


def verify_indexes(
    samples: Samples,
    probes: IndexProbes,
    *,
    required_layers: Sequence[str] | None = None,
) -> None:
    layers = (
        frozenset(required_layers)
        if required_layers is not None
        else frozenset({"code_index", "graph"})
    )
    need_code = "code_index" in layers
    need_graph = "graph" in layers
    graph = getattr(probes, "graph", None)

    if samples.is_empty():
        if samples.integrity_unknown:
            raise RuntimeError(
                "empty diff with unknown integrity is not success"
            )
        _verify_empty_layer_status(probes, need_code=need_code, need_graph=need_graph)
        return

    code_checked = False
    graph_checked = False

    if need_code:
        for path in samples.bsl_paths:
            probes.code_index.check_bsl(path)
            code_checked = True
    if need_graph:
        if graph is None:
            raise RuntimeError("graph probe required for this database")
        for path in samples.bsl_paths:
            if getattr(graph, "check_bsl_cmd", None):
                graph.check_bsl(path)
                graph_checked = True
        for path in samples.xml_paths:
            graph.check_xml(path)
            graph_checked = True
    for path in samples.deletions:
        if path.endswith(".bsl"):
            if need_code:
                probes.code_index.check_deletion(path)
                code_checked = True
            if need_graph and graph is not None:
                graph.check_deletion(path)
                graph_checked = True
        elif path.endswith(".xml"):
            if need_graph and graph is not None:
                graph.check_deletion(path)
                graph_checked = True
        else:
            if need_code:
                probes.code_index.check_deletion(path)
                code_checked = True
            if need_graph and graph is not None:
                graph.check_deletion(path)
                graph_checked = True

    # Missing suitable samples for a required layer is not silent success:
    # use the explicit empty-index status contract instead.
    if need_code and not code_checked:
        if probes.code_index.status != "ready":
            raise RuntimeError(
                f"code_index probe not ready ({probes.code_index.status}); "
                "no BSL samples were available to check"
            )
    if need_graph and not graph_checked:
        if graph is None:
            raise RuntimeError("graph probe required for this database")
        if graph.status != "ready":
            raise RuntimeError(
                f"graph probe not ready ({graph.status}); "
                "no Graph samples were available to check"
            )
