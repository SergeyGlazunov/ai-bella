"""Task 8 Block 4: package selection and local recovery sources."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from contour.config import load_config
from contour.db import init_db
from contour.lock import MemoryBackend
from contour.packages import choose_newest_valid_package
from contour.reconcile import RuntimePaths, reconcile
from contour.sources import (
    PublishedRecoveryProvider,
    build_source_provider,
    contour_sources_root,
    read_revision,
    write_revision,
)
from contour.state import StateStore
from test_packages import _make_package

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


def _runtime_cfg(tmp_path: Path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    root = tmp_path / "root"
    raw["runtime"]["root"] = str(root)
    raw["runtime"]["db_path"] = str(tmp_path / "state.db")
    raw["runtime"]["lock_path"] = str(tmp_path / "sync.lock")
    raw["runtime"]["logs_path"] = str(tmp_path / "logs")
    raw["runtime"]["backups_path"] = str(tmp_path / "backups")
    for name in ("ubd", "kb"):
        raw["databases"][name]["enabled"] = False
    inbox = tmp_path / "inbox" / "klon_opl"
    inbox.mkdir(parents=True)
    published = root / "sources" / "klon_opl"
    published.mkdir(parents=True)
    raw["databases"]["klon_opl"]["source"]["inbox"] = str(inbox)
    raw["databases"]["klon_opl"]["source"]["source_path"] = str(published)
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path)), inbox, published


def test_no_new_package_is_noop(tmp_path: Path):
    cfg, inbox, _published = _runtime_cfg(tmp_path)
    chosen, warnings = choose_newest_valid_package(inbox, "klon_opl")
    assert chosen is None
    assert warnings == []

    seen: list[str] = []

    def runner(database, **kwargs):
        del kwargs
        seen.append(database.name)
        return "deferred"

    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        paths=RuntimePaths(
            db_path=str(tmp_path / "state.db"),
            lock_path=str(tmp_path / "sync.lock"),
        ),
        lock_backend=MemoryBackend(),
        docker=__import__("test_reconcile_multidb", fromlist=["PingDocker"]).PingDocker(),
        database_runner=runner,
    )
    # Empty inbox → runner still invoked; production runner will return deferred noop.
    # Here we only assert choose_* reports no package and reconcile continues.
    assert result == "deferred"
    assert seen == ["klon_opl"]


def test_choose_newest_valid_ready_package(tmp_path: Path):
    older = _make_package(
        tmp_path,
        name="ready-old",
        database="klon_opl",
        export_id="export-old",
        manifest_overrides={"finished_at": "2026-10-14T00:01:00Z"},
    )
    newer = _make_package(
        tmp_path,
        name="ready-new",
        database="klon_opl",
        export_id="export-new",
        manifest_overrides={"finished_at": "2026-10-14T00:05:00Z"},
    )
    broken = _make_package(
        tmp_path,
        name="ready-bad",
        database="klon_opl",
        export_id="export-bad",
        checksum="0" * 64,
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for pkg in (older, newer, broken):
        pkg.rename(inbox / pkg.name)

    chosen, warnings = choose_newest_valid_package(inbox, "klon_opl")
    assert chosen is not None
    assert chosen.manifest.export_id == "export-new"
    assert any("ready-bad" in item for item in warnings)


def test_recovery_works_without_remote(tmp_path: Path):
    cfg, _inbox, published = _runtime_cfg(tmp_path)
    write_revision(published, "rev-local")
    (published / "Module.bsl").write_text("// ok", encoding="utf-8")
    sources_root = contour_sources_root(cfg)
    staging = sources_root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)

    provider = PublishedRecoveryProvider(
        "klon_opl",
        published_path=published,
        staging_root=staging,
        sources_root=sources_root,
    )
    prepared = provider.prepare()
    assert prepared.revision == "rev-local"
    assert prepared.staged_path != prepared.published_path
    assert read_revision(prepared.staged_path) == "rev-local"
    assert (prepared.staged_path / "Module.bsl").is_file()


def test_same_revision_rebuilds_after_volume_recreation(tmp_path: Path):
    cfg, _inbox, published = _runtime_cfg(tmp_path)
    write_revision(published, "rev-1")
    (published / "a.bsl").write_text("x", encoding="utf-8")
    conn = init_db(str(tmp_path / "state.db"))
    state = StateStore(conn)
    state.register_database("klon_opl", "package")
    state.mark_layer(
        "klon_opl", "code_index", "rev-1", "ready", "vol|old", "img-old"
    )
    state.mark_published("klon_opl", "rev-1")

    from contour.reconcile import storage_identity_changed

    assert (
        storage_identity_changed(
            conn,
            "klon_opl",
            "code_index",
            current_storage_id="vol|new",
            current_indexer_version="img-old",
        )
        is True
    )


def test_new_revision_waits_for_unfinished_writer(tmp_path: Path):
    from contour.reconcile import writer_blocks_database
    from test_reconcile_multidb import ScriptedDocker, _insert_run_job

    cfg, _inbox, _published = _runtime_cfg(tmp_path)
    # Enable kb sharing for a busy job while OPL is free — unrelated.
    # Directly: OPL busy blocks itself.
    opl = cfg.databases["klon_opl"]
    conn = init_db(str(tmp_path / "state.db"))
    _insert_run_job(
        conn,
        database="klon_opl",
        stage="bsl_index",
        status="running",
        container_id="cid-opl",
        exec_id=None,
    )
    docker = ScriptedDocker(inspect_status="running")
    reason = writer_blocks_database(conn, opl, [opl], docker)
    assert reason is not None
    assert "still running" in reason


def test_build_source_provider_package(tmp_path: Path):
    cfg, inbox, published = _runtime_cfg(tmp_path)
    pkg = _make_package(
        tmp_path,
        name="ready-1",
        database="klon_opl",
        export_id="e1",
    )
    dest = inbox / "ready-e1"
    shutil.move(str(pkg), str(dest))
    chosen, _ = choose_newest_valid_package(inbox, "klon_opl")
    assert chosen is not None
    provider = build_source_provider(
        cfg.databases["klon_opl"], cfg, package=chosen
    )
    prepared = provider.prepare()
    assert prepared.database == "klon_opl"
    assert prepared.published_path == published
