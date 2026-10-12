from __future__ import annotations

import subprocess
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
    repo: str | Path, *, workspace_subdir: str = "", limit: int = 32
) -> Samples:
    """Recovery samples from working tree under workspace_subdir (capped)."""
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
            if posix.endswith(".bsl"):
                bsl_paths.append(posix)
            elif posix.endswith(".xml"):
                xml_paths.append(posix)
            if len(bsl_paths) + len(xml_paths) >= limit:
                break
    return Samples(
        bsl_paths=bsl_paths,
        xml_paths=xml_paths,
        deletions=[],
        integrity_unknown=True,
    )


def verify_indexes(samples: Samples, probes: IndexProbes) -> None:
    if samples.is_empty():
        if samples.integrity_unknown:
            raise RuntimeError(
                "empty diff with unknown integrity is not success"
            )
        if probes.code_index.status != "ready":
            raise RuntimeError(
                f"code_index probe not ready ({probes.code_index.status})"
            )
        if probes.graph.status != "ready":
            raise RuntimeError(f"graph probe not ready ({probes.graph.status})")
        return

    for path in samples.bsl_paths:
        probes.code_index.check_bsl(path)
        if getattr(probes.graph, "check_bsl_cmd", None):
            probes.graph.check_bsl(path)
    for path in samples.xml_paths:
        probes.graph.check_xml(path)
    for path in samples.deletions:
        if path.endswith(".bsl"):
            probes.code_index.check_deletion(path)
            probes.graph.check_deletion(path)
        elif path.endswith(".xml"):
            probes.graph.check_deletion(path)
        else:
            probes.code_index.check_deletion(path)
            probes.graph.check_deletion(path)
