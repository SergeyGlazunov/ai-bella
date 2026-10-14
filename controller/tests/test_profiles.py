"""Database profiles and layer adapter contracts (Task 6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from contour.config import (
    AdapterCommands,
    AdaptersConfig,
    DatabaseConfig,
    GitSourceConfig,
    PackageSourceConfig,
    ServicesConfig,
    StorageConfig,
    load_config,
)
from contour.db import init_db
from contour.profiles import (
    DatabaseProfile,
    LayerResult,
    profile_from_config,
    run_bsl_layer,
    run_graph_layer,
    run_required_layers,
)
from test_index_graph import FakeDockerRunner

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


def _services(**overrides) -> ServicesConfig:
    base = dict(
        graph_compose_dir="/tmp/graph",
        graph_project="graph",
        graph_service="metadata-indexer",
        graph_mcp="mcp",
        bsl_compose_dir="/tmp/bsl",
        bsl_project="bsl",
        bsl_container="bsl",
        bsl_publish_port=8011,
        bsl_daemon_toml="/tmp/bsl/daemon.toml",
        webui_network="net",
    )
    base.update(overrides)
    return ServicesConfig(**base)


def _storage() -> StorageConfig:
    return StorageConfig(graph_volume="g", code_index_volume="c")


def _db(
    name: str,
    layers: frozenset[str],
    *,
    source: GitSourceConfig | PackageSourceConfig | None = None,
) -> DatabaseConfig:
    if source is None:
        source = GitSourceConfig("git", "/repo", "origin", "main", "")
    return DatabaseConfig(
        name=name,
        source=source,
        required_layers=layers,
        services=_services(),
        storage=_storage(),
        adapters=AdaptersConfig(
            code_index=AdapterCommands(),
            graph=AdapterCommands(),
            idle_probe=[],
        ),
    )


def test_ubd_and_kb_require_code_index_then_graph():
    for name in ("ubd", "kb"):
        profile = profile_from_config(
            _db(name, frozenset({"graph", "code_index"}))
        )
        assert isinstance(profile, DatabaseProfile)
        assert profile.name == name
        assert profile.required_layers == ("code_index", "graph")


def test_klon_opl_requires_only_code_index():
    profile = profile_from_config(_db("klon_opl", frozenset({"code_index"})))
    assert profile.required_layers == ("code_index",)


def test_unknown_layer_is_rejected():
    with pytest.raises(ValueError, match="unknown"):
        profile_from_config(_db("ubd", frozenset({"code_index", "mystery"})))


def test_skipped_required_layer_raises(tmp_path: Path):
    db = _db("klon_opl", frozenset({"code_index"}))
    conn = init_db(str(tmp_path / "s.db"))
    conn.execute(
        "INSERT INTO runs (database, target_revision, started_at) VALUES (?,?,?)",
        ("klon_opl", "rev-1", "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    with pytest.raises(ValueError, match="required layer"):
        run_required_layers(
            db,
            revision="rev-1",
            run_id=run_id,
            conn=conn,
            runner=FakeDockerRunner(),
            only_layers=("graph",),
        )


def test_jobs_record_database_and_revision_before_start(tmp_path: Path):
    cfg = load_config(EXAMPLE)
    assert cfg.databases is not None
    database = cfg.databases["ubd"]
    conn = init_db(str(tmp_path / "s.db"))
    conn.execute(
        "INSERT INTO runs (database, target_revision, started_at) VALUES (?,?,?)",
        ("ubd", "abc123", "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    runner = FakeDockerRunner(conn=conn)
    result = run_bsl_layer(
        database,
        revision="abc123",
        run_id=run_id,
        conn=conn,
        runner=runner,
    )
    assert isinstance(result, LayerResult)
    assert result.layer == "code_index"
    assert result.revision == "abc123"

    # Before any start_exec the job row must already name database + join to revision.
    for exec_id, job_exec in runner.job_id_at_exec_start.items():
        assert job_exec == exec_id
    rows = conn.execute(
        """
        SELECT j.database AS job_db, j.status, r.target_revision
        FROM jobs j
        JOIN runs r ON r.id = j.run_id
        WHERE j.run_id = ?
        ORDER BY j.id
        """,
        (run_id,),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["job_db"] == "ubd"
        assert row["target_revision"] == "abc123"


def test_graph_layer_preserves_exit_code_3_full_contract(tmp_path: Path):
    cfg = load_config(EXAMPLE)
    assert cfg.databases is not None
    database = cfg.databases["ubd"]
    conn = init_db(str(tmp_path / "s.db"))
    conn.execute(
        "INSERT INTO runs (database, target_revision, started_at) VALUES (?,?,?)",
        ("ubd", "abc123", "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    runner = FakeDockerRunner(conn=conn, wait_codes=[3, 0])
    result = run_graph_layer(
        database,
        revision="abc123",
        run_id=run_id,
        conn=conn,
        runner=runner,
        may_write=lambda: "ok",
    )
    assert result.layer == "graph"
    assert result.revision == "abc123"
    stages = [
        row["stage"]
        for row in conn.execute(
            "SELECT stage FROM jobs WHERE run_id = ? ORDER BY id", (run_id,)
        )
    ]
    assert "graph" in stages
    assert "graph_full" in stages
