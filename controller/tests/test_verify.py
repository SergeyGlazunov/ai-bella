from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from contour.verify import Samples, sample_current_tree, sample_paths, verify_indexes


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@dataclass
class FakeProbe:
    status: str
    bsl_checked: list[str] = field(default_factory=list)
    xml_checked: list[str] = field(default_factory=list)
    deletions_checked: list[str] = field(default_factory=list)

    def check_bsl(self, path: str) -> None:
        self.bsl_checked.append(path)

    def check_xml(self, path: str) -> None:
        self.xml_checked.append(path)

    def check_deletion(self, path: str) -> None:
        self.deletions_checked.append(path)


@dataclass
class FakeProbes:
    code_index: FakeProbe
    graph: FakeProbe


def test_sample_paths_classifies_bsl_xml_and_deletion(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "src" / "CommonModules").mkdir(parents=True)
    (repo / "src" / "CommonModules" / "Foo.bsl").write_text("// foo", encoding="utf-8")
    (repo / "src" / "Catalogs").mkdir(parents=True)
    (repo / "src" / "Catalogs" / "Items.xml").write_text("<Meta/>", encoding="utf-8")
    (repo / "src" / "removed.xml").write_text("<Old/>", encoding="utf-8")
    old_sha = _commit_all(repo, "initial")

    (repo / "src" / "CommonModules" / "Foo.bsl").write_text("// changed", encoding="utf-8")
    (repo / "src" / "Catalogs" / "Items.xml").write_text("<Meta updated/>", encoding="utf-8")
    (repo / "src" / "removed.xml").unlink()
    new_sha = _commit_all(repo, "second")

    samples = sample_paths(repo, old_sha, new_sha, workspace_subdir="src")

    assert samples.bsl_paths == ["CommonModules/Foo.bsl"]
    assert samples.xml_paths == ["Catalogs/Items.xml"]
    assert samples.deletions == ["removed.xml"]


def test_verify_bsl_deletion_also_checks_graph():
    samples = Samples(
        bsl_paths=[],
        xml_paths=[],
        deletions=["CommonModules/Gone/Ext/Module.bsl"],
    )
    code = FakeProbe(status="ready")
    graph = FakeProbe(status="ready")
    verify_indexes(samples, FakeProbes(code_index=code, graph=graph))
    assert code.deletions_checked == ["CommonModules/Gone/Ext/Module.bsl"]
    assert graph.deletions_checked == ["CommonModules/Gone/Ext/Module.bsl"]


def test_verify_indexes_empty_diff_with_integrity_unknown_raises():
    samples = Samples(
        bsl_paths=[],
        xml_paths=[],
        deletions=[],
        integrity_unknown=True,
    )
    probes = FakeProbes(
        code_index=FakeProbe(status="ready"),
        graph=FakeProbe(status="ready"),
    )

    with pytest.raises(RuntimeError, match="(?i)integrity|unknown|empty"):
        verify_indexes(samples, probes)


def test_verify_indexes_empty_diff_all_ready_ok_when_integrity_known():
    samples = Samples(
        bsl_paths=[],
        xml_paths=[],
        deletions=[],
        integrity_unknown=False,
    )
    probes = FakeProbes(
        code_index=FakeProbe(status="ready"),
        graph=FakeProbe(status="ready"),
    )

    verify_indexes(samples, probes)


def test_verify_indexes_empty_diff_not_ready_raises():
    samples = Samples(
        bsl_paths=[],
        xml_paths=[],
        deletions=[],
        integrity_unknown=False,
    )
    probes = FakeProbes(
        code_index=FakeProbe(status="ready"),
        graph=FakeProbe(status="not_ready"),
    )

    with pytest.raises(RuntimeError, match="(?i)ready|probe|graph"):
        verify_indexes(samples, probes)


def test_verify_indexes_dispatches_samples_to_probes():
    samples = Samples(
        bsl_paths=["a.bsl"],
        xml_paths=["b.xml"],
        deletions=["c.xml"],
        integrity_unknown=False,
    )
    code = FakeProbe(status="ready")
    graph = FakeProbe(status="ready")
    probes = FakeProbes(code_index=code, graph=graph)

    verify_indexes(samples, probes)

    assert code.bsl_checked == ["a.bsl"]
    assert graph.xml_checked == ["b.xml"]
    assert graph.deletions_checked == ["c.xml"]
    assert code.deletions_checked == []


def test_verify_indexes_routes_bsl_deletion_to_code_index():
    samples = Samples(
        bsl_paths=[],
        xml_paths=[],
        deletions=["src/Foo.bsl", "Catalogs/Items.xml"],
        integrity_unknown=False,
    )
    code = FakeProbe(status="ready")
    graph = FakeProbe(status="ready")
    verify_indexes(samples, FakeProbes(code_index=code, graph=graph))
    assert code.deletions_checked == ["src/Foo.bsl"]
    assert graph.deletions_checked == ["src/Foo.bsl", "Catalogs/Items.xml"]


def test_sample_current_tree_empty_unknown_fails(tmp_path: Path):
    repo = tmp_path / "empty-tree"
    repo.mkdir()
    samples = sample_current_tree(repo)
    assert samples.is_empty()
    assert samples.integrity_unknown is True
    probes = FakeProbes(
        code_index=FakeProbe(status="ready"),
        graph=FakeProbe(status="ready"),
    )
    with pytest.raises(RuntimeError, match="(?i)integrity|unknown|empty"):
        verify_indexes(samples, probes)
