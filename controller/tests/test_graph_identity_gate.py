"""Graph layer identity gate and recovery visibility.

Root cause (2026-10-26, stand ADM-SAPHX-TECH): run_graph_layer recorded
the WRITER job image as layer identity, while the recovery gate
(_live_layer_identities) compares against the SERVING graph MCP image.
Writer and MCP are different containers, so after the MCP image was
rebuilt the gate reported "changed" every night → git sources entered
the PublishedRecoveryProvider branch forever → git fetch never ran and
UBD stayed on a 10-day-old revision behind green "published" runs.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import contour.dockerutil as dockerutil
import contour.profiles as profiles_mod
import contour.reconcile as reconcile_mod
from contour.config import load_config
from contour.db import init_db
from contour.lock import MemoryBackend
from contour.pipeline import PipelineResult
from contour.reconcile import RuntimePaths, reconcile
from contour.state import StateStore
from test_reconcile_multidb import _in_window, _v2_config

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


class IdentityDocker:
    """Fake engine answering identity probes; empty stdout for the rest."""

    def __init__(self, volumes: dict[str, str], images: dict[str, str]):
        self.volumes = volumes
        self.images = images

    def run(self, cmd: list[str], *, timeout: float | None = None):
        import subprocess

        if cmd[:2] == ["docker", "volume"] and "inspect" in cmd:
            name = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=self.volumes.get(name, ""), stderr=""
            )
        if cmd[:2] == ["docker", "inspect"] and "{{.Image}}" in cmd:
            name = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=self.images.get(name, ""), stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _example_database():
    cfg = load_config(str(EXAMPLE))
    assert cfg.databases is not None
    return cfg.databases["ubd"]


def _start_run(conn, database: str, revision: str) -> int:
    conn.execute(
        "INSERT INTO runs (database, target_revision, started_at) "
        "VALUES (?,?,?)",
        (database, revision, "2026-10-26T00:00:00Z"),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


# --- Fix 2: the graph layer identity is the serving MCP image -------------


def test_run_graph_layer_records_serving_mcp_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The recorded graph identity must come from the graph MCP container,
    the same object the recovery gate reads — not from the writer job."""
    database = _example_database()
    conn = init_db(str(tmp_path / "s.db"))
    run_id = _start_run(conn, database.name, "rev1")

    monkeypatch.setattr(
        profiles_mod, "index_graph", lambda *a, **k: "writer-job-container"
    )
    asked: list[str] = []

    def fake_image_id(container, *, runner=None):
        asked.append(container)
        return f"img:{container}"

    def fake_volume_identity(volume, *, runner=None):
        return f"vol:{volume}"

    monkeypatch.setattr(dockerutil, "image_id", fake_image_id)
    monkeypatch.setattr(dockerutil, "volume_identity", fake_volume_identity)

    result = profiles_mod.run_graph_layer(
        database, revision="rev1", run_id=run_id, conn=conn
    )

    assert result.layer == "graph"
    assert result.storage_id == f"vol:{database.storage.graph_volume}"
    # The identity is the serving MCP container image, and the writer
    # image is not even queried.
    assert result.indexer_version == f"img:{database.services.graph_mcp}"
    assert "writer-job-container" not in asked


def test_gate_matches_when_recorded_identity_is_mcp_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end gate semantic: when the recorded graph identity is the
    MCP image (as run_graph_layer now records), a future writer running a
    different image does not turn the gate into "changed"."""
    database = _example_database()
    conn = init_db(str(tmp_path / "s.db"))
    storage = "g|2026-01-01T00:00:00Z"
    mcp_image = "img:mcp"
    for layer, version, volume in (
        ("graph", mcp_image, database.storage.graph_volume),
        ("code_index", "img:bsl", database.storage.code_index_volume),
    ):
        conn.execute(
            "INSERT INTO layer_state "
            "(database, layer, status, applied_revision, storage_id, "
            " indexer_version, updated_at) VALUES (?,?,?,?,?,?,?)",
            (database.name, layer, "ready", "rev1", f"{volume}|x", version,
             "2026-01-01T00:00:00Z"),
        )
    conn.commit()

    def fake_image_id(container, *, runner=None):
        return mcp_image if container == database.services.graph_mcp else "img:bsl"

    def fake_volume_identity(volume, *, runner=None):
        return f"{volume}|x"

    monkeypatch.setattr(dockerutil, "image_id", fake_image_id)
    monkeypatch.setattr(dockerutil, "volume_identity", fake_volume_identity)

    status = reconcile_mod._live_storage_status(database, "rev1", conn, None)
    assert status == "match"


# --- Fix 3: git-source recovery must be visible ----------------------------


class _FakeGit:
    def __init__(self, origin_rev: str | None, error: Exception | None = None):
        self.origin_rev = origin_rev
        self.error = error
        self.fetched: list[str] = []

    def fetch(self, repo):
        self.fetched.append(str(repo))
        if self.error is not None:
            raise self.error

    def rev_parse(self, repo, ref: str) -> str:
        assert ref.startswith("origin/")
        return self.origin_rev


def test_recovery_note_upstream_ahead():
    database = _example_database()
    git = _FakeGit(origin_rev="b" * 40)
    note = reconcile_mod._recovery_source_note(
        database, published_revision="a" * 40, git=git
    )
    assert database.name in note
    assert "recovery" in note
    assert ("bbbbbbbb" in note) or ("ahead" in note)
    assert git.fetched == [str(Path(getattr(database.source, "path", "")))]


def test_recovery_note_upstream_unchanged():
    database = _example_database()
    git = _FakeGit(origin_rev="a" * 40)
    note = reconcile_mod._recovery_source_note(
        database, published_revision="a" * 40, git=git
    )
    assert "recovery" in note
    assert "unchanged" in note


def test_recovery_note_fetch_failure_degrades_not_raises():
    database = _example_database()
    git = _FakeGit(origin_rev=None, error=RuntimeError("no network"))
    note = reconcile_mod._recovery_source_note(
        database, published_revision="a" * 40, git=git
    )
    assert "recovery" in note
    assert "fetch failed" in note


def test_git_recovery_branch_alerts_and_skips_fetch_of_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Wiring: a git-source database that enters the recovery branch sends
    the source note through the alert channel. (The incident detector.)"""
    cfg = _v2_config(tmp_path)
    database = cfg.databases["ubd"]
    assert database.source.path and database.source.branch

    conn = init_db(str(tmp_path / "state.db"))
    state = StateStore(conn)
    state.register_database("ubd", "git")
    conn.execute(
        "INSERT INTO layer_state "
        "(database, layer, status, applied_revision, storage_id, "
        " indexer_version, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("ubd", "graph", "ready", "rev-old", "g|x", "img-old",
         "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO layer_state "
        "(database, layer, status, applied_revision, storage_id, "
        " indexer_version, updated_at) VALUES (?,?,?,?,?,?,?)",
        ("ubd", "code_index", "ready", "rev-old", "c|x", "img-old",
         "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    # For a git database the published path IS the source repo path
    # (published_path_for returns Path(source.path)); _v2_config already
    # created it as an empty dir. Drop the revision marker there so the
    # recovery branch becomes reachable.
    published = Path(database.source.path)
    published.mkdir(parents=True, exist_ok=True)
    (published / ".contour-revision").write_text(
        "rev-old", encoding="utf-8"
    )

    docker = IdentityDocker(
        volumes={
            database.storage.graph_volume: "g|x",
            database.storage.code_index_volume: "c|x",
        },
        images={
            database.services.graph_mcp: "img-new",
            database.services.bsl_container: "img-new",
        },
    )

    # The real build_source_provider would create the sources root; the
    # monkeypatched fake does not — create the runtime root so the free
    # space preflight (disk_usage) does not trip on a missing path.
    Path(cfg.runtime.root).mkdir(parents=True, exist_ok=True)

    alerts: list[str] = []
    provider_calls: list[dict] = []
    pipeline_calls: list[str] = []

    def fake_build_source_provider(database, cfg, **kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(prepare=lambda: SimpleNamespace(
            revision="rev-old"))

    import contour.pipeline as pipeline_mod

    def fake_pipeline(database, source, *args, **kwargs):
        pipeline_calls.append(database.name)
        return PipelineResult(database.name, "rev-old", "published", "ok")

    import contour.sources as sources_mod

    monkeypatch.setattr(
        sources_mod, "build_source_provider", fake_build_source_provider
    )
    monkeypatch.setattr(
        pipeline_mod, "run_database_pipeline", fake_pipeline
    )

    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=_in_window(),
        paths=RuntimePaths(
            db_path=str(tmp_path / "state.db"),
            lock_path=str(tmp_path / "sync.lock"),
        ),
        lock_backend=MemoryBackend(),
        docker=docker,
        alert_send=lambda cfg_, text: alerts.append(text),
    )

    # kb/klon_opl have no packages → deferred; aggregate keeps "deferred".
    # The ubd signals below are what this test exists for.
    assert result == "deferred", alerts
    assert pipeline_calls == ["ubd"]
    assert provider_calls and provider_calls[0].get("recovery") is True
    assert any(
        "recovery from published" in text for text in alerts
    ), alerts
