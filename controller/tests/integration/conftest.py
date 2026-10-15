"""Shared fixtures for linux-first acceptance (no live Docker / stand)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pytest
import yaml

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
from contour.pipeline import run_database_pipeline
from contour.profiles import LayerResult
from contour.sources import PreparedSource, compute_staged_tree_digest, write_revision
from contour.state import StateStore
from contour.verify import Samples

EXAMPLE = Path(__file__).resolve().parents[3] / "contour.example.yaml"
VLAD = ZoneInfo("Asia/Vladivostok")


@pytest.fixture
def in_window() -> datetime:
    return datetime(2026, 9, 20, 3, 0, tzinfo=VLAD)


def _services(tmp_path: Path, name: str) -> ServicesConfig:
    return ServicesConfig(
        graph_compose_dir=str(tmp_path / f"compose-graph-{name}"),
        graph_project=f"graph-{name}",
        graph_service="metadata-indexer",
        graph_mcp=f"mcp-{name}",
        bsl_compose_dir=str(tmp_path / f"compose-bsl-{name}"),
        bsl_project=f"bsl-{name}",
        bsl_container=f"bsl-{name}",
        bsl_publish_port=8011,
        bsl_daemon_toml=str(tmp_path / f"daemon-{name}.toml"),
        webui_network="net",
        neo4j_container="",
    )


def database_config(
    tmp_path: Path,
    name: str,
    *,
    layers: frozenset[str],
    kind: str = "package",
) -> DatabaseConfig:
    services = _services(tmp_path, name)
    storage = StorageConfig(
        graph_volume=f"g-{name}",
        code_index_volume=f"c-{name}",
    )
    adapters = AdaptersConfig(
        code_index=AdapterCommands(),
        graph=AdapterCommands(),
        idle_probe=["true"],
    )
    if kind == "git":
        source = GitSourceConfig(
            "git",
            str(tmp_path / "sources" / name),
            "https://example.invalid/ubd.git",
            "main",
            "src",
        )
    else:
        source = PackageSourceConfig(
            "package",
            str(tmp_path / "inbox" / name),
            str(tmp_path / "sources" / name),
        )
    return DatabaseConfig(
        name=name,
        source=source,
        required_layers=layers,
        services=services,
        storage=storage,
        adapters=adapters,
        enabled=True,
    )


def prepared_source(
    tmp_path: Path, database: str, revision: str
) -> PreparedSource:
    root = tmp_path / "sources"
    staging = root / ".staging" / database / "tok"
    published = root / database
    root.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    write_revision(staging, revision)
    (staging / "Catalogs").mkdir(exist_ok=True)
    (staging / "Catalogs" / "Foo.xml").write_text("<Catalog/>", encoding="utf-8")
    (staging / "data.txt").write_text(f"payload-{revision}", encoding="utf-8")
    return PreparedSource(
        database=database,
        revision=revision,
        staged_path=staging,
        previous_path=None,
        sources_root=root,
        published_path=published,
        kind="package",
        content_digest=compute_staged_tree_digest(staging),
    )


@dataclass
class RecordingReads:
    closed: bool = False
    events: list[str] = field(default_factory=list)

    def close(self) -> None:
        self.closed = True
        self.events.append("close")

    def open(self) -> None:
        self.closed = False
        self.events.append("open")

    def is_closed(self) -> bool:
        return self.closed


@dataclass
class FakeSource:
    prepared: PreparedSource
    fail_prepare: bool = False

    def prepare(self) -> PreparedSource:
        if self.fail_prepare:
            raise RuntimeError("prepare failed")
        return self.prepared


@dataclass
class FakeLayers:
    results: list[LayerResult] | None = None
    fail_after: int | None = None
    fail_layer: str | None = None
    seen_full_needed: bool | None = None
    events: list[str] = field(default_factory=list)

    def run(
        self,
        database: DatabaseConfig,
        revision: str,
        run_id: int,
        *,
        full_needed: bool = False,
        may_write: Callable[[], str] | None = None,
        on_layer_start: Callable[[str], None] | None = None,
        on_layer_result: Callable[[LayerResult], None] | None = None,
        on_layer_error: Callable[[str, BaseException], None] | None = None,
    ) -> list[LayerResult]:
        del may_write, run_id
        self.seen_full_needed = full_needed
        self.events.append("index")
        planned = (
            list(self.results)
            if self.results is not None
            else [
                LayerResult(
                    layer=layer,
                    revision=revision,
                    storage_id=f"{layer}-storage",
                    indexer_version=f"{layer}-image",
                )
                for layer in ("code_index", "graph")
                if layer in database.required_layers
            ]
        )
        out: list[LayerResult] = []
        for item in planned:
            if on_layer_start is not None:
                on_layer_start(item.layer)
            try:
                if self.fail_layer is not None and item.layer == self.fail_layer:
                    raise RuntimeError(f"{item.layer} failed")
                if self.fail_after is not None and len(out) + 1 == self.fail_after:
                    raise RuntimeError(f"{item.layer} failed")
                out.append(item)
                if on_layer_result is not None:
                    on_layer_result(item)
            except Exception as exc:
                if on_layer_error is not None:
                    on_layer_error(item.layer, exc)
                raise
        return out


@dataclass
class FakeProbe:
    status: str = "ready"
    events: list[str] = field(default_factory=list)

    def check_bsl(self, path: str) -> None:
        self.events.append(("bsl", path))

    def check_xml(self, path: str) -> None:
        self.events.append(("xml", path))

    def check_deletion(self, path: str) -> None:
        self.events.append(("del", path))


@dataclass
class FakeProbes:
    code_index: FakeProbe = field(default_factory=FakeProbe)
    graph: FakeProbe | None = field(default_factory=FakeProbe)
    fail: bool = False
    ran: bool = False
    events: list[str] = field(default_factory=list)

    def run_before_open(
        self, samples: Samples, required_layers=None
    ) -> list[str]:
        self.ran = True
        self.events.append("probes")
        if self.fail:
            raise RuntimeError("probe failed")
        from contour.verify import verify_indexes

        return list(
            verify_indexes(samples, self, required_layers=required_layers) or []
        )


def run_publish_cycle(
    tmp_path: Path,
    name: str,
    *,
    layers: frozenset[str],
    revision: str = "r1",
    kind: str = "package",
    layer_runner: FakeLayers | None = None,
    probes: FakeProbes | None = None,
    reads: RecordingReads | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(tmp_path / f"state-{name}.db"))
    state = StateStore(conn)
    source_kind = "git" if kind == "git" else "package"
    state.register_database(name, source_kind)
    database = database_config(tmp_path, name, layers=layers, kind=kind)
    prepared = prepared_source(tmp_path, name, revision)
    reads = reads or RecordingReads()
    layers_runner = layer_runner or FakeLayers()
    probes = probes or FakeProbes()
    result = run_database_pipeline(
        database,
        FakeSource(prepared),
        state,
        reads,
        layers_runner,
        probes,
        conn=conn,
        samples=Samples(bsl_paths=["a.bsl"], xml_paths=["a.xml"], deletions=[]),
    )
    return conn, state, database, prepared, reads, probes, result


def load_v2_runtime_config(tmp_path: Path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    root = tmp_path / "root"
    root.mkdir()
    raw["runtime"]["root"] = str(root)
    raw["runtime"]["db_path"] = str(tmp_path / "state.db")
    raw["runtime"]["lock_path"] = str(tmp_path / "sync.lock")
    raw["runtime"]["logs_path"] = str(tmp_path / "logs")
    raw["runtime"]["backups_path"] = str(tmp_path / "backups")
    for name in ("kb", "klon_opl"):
        inbox = tmp_path / "inbox" / name
        inbox.mkdir(parents=True)
        raw["databases"][name]["source"]["inbox"] = str(inbox)
        raw["databases"][name]["source"]["source_path"] = str(
            tmp_path / "sources" / name
        )
    ubd = tmp_path / "sources" / "ubd"
    ubd.mkdir(parents=True)
    raw["databases"]["ubd"]["source"]["path"] = str(ubd)
    path = tmp_path / "contour.yaml"
    path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(path)), path
