"""Task 11 review: production _default_v2_database_runner with fake Docker only.

Quarantined: full-module run hung the host/VM (2026-10-16). Re-enable only as a
tiny single-case test in a later step — do not run this file as-is.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from contour.db import init_db
from contour.lock import MemoryBackend
from contour.reconcile import RuntimePaths, reconcile
from contour.sources import read_revision
from test_packages import _make_package

EXAMPLE = Path(__file__).resolve().parents[3] / "contour.example.yaml"
VLAD = ZoneInfo("Asia/Vladivostok")
IN_WINDOW = datetime(2026, 9, 20, 3, 0, tzinfo=VLAD)

pytestmark = pytest.mark.skip(
    reason="quarantined: hung host/VM; rewrite as tiny single-case later",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _write_daemon_toml(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[[paths]]\npath = "/repos/SRC"\n\n[server]\n',
        encoding="utf-8",
    )


def _write_compose(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "docker-compose.yml").write_text(
        "services:\n  metadata-indexer:\n    image: alpine\n",
        encoding="utf-8",
    )


def _seed_ubd_git(tmp_path: Path) -> tuple[Path, Path, str]:
    """Bare remote + clone under sources/ubd with src/ tree for probes."""
    remote = tmp_path / "ubd-remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")
    work = tmp_path / "ubd-seed"
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    src = work / "src"
    src.mkdir()
    (src / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    (src / "Catalogs").mkdir()
    (src / "Catalogs" / "Foo.xml").write_text("<Catalog/>", encoding="utf-8")
    (src / "CommonModule.bsl").write_text("// probe\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "seed")
    _git(work, "push", "-u", "origin", "HEAD")
    branch = _git(work, "symbolic-ref", "--short", "HEAD").stdout.strip()
    published = tmp_path / "root" / "sources" / "ubd"
    if published.exists():
        shutil.rmtree(published)
    _git(tmp_path, "clone", str(remote), str(published))
    _git(published, "config", "user.email", "t@example.com")
    _git(published, "config", "user.name", "T")
    return remote, published, branch


@dataclass
class IntegrationDocker:
    """Fake Engine at the Docker boundary: index jobs, reads, probes, volumes."""

    logs_dir: Path
    calls: list[list[str]] = field(default_factory=list)
    graph_status: dict[str, str] = field(default_factory=dict)
    connected_networks: dict[str, set[str]] = field(default_factory=dict)
    exec_ids: list[str] = field(default_factory=list)
    exec_exit: dict[str, int] = field(default_factory=dict)
    _exec_i: int = 0
    default_network: str = "ai-stack_default"

    def idle_probe(self) -> bool:
        return True

    def wait(self, container_id: str) -> int:
        """Short-circuit dockerutil.wait if it prefers a bound method."""
        del container_id
        return 0

    def create_exec(
        self, container: str, cmd: list[str], env: list[str] | None = None
    ) -> str:
        self.calls.append(["create_exec", container, *(env or []), *cmd])
        self._exec_i += 1
        exec_id = f"exec-{self._exec_i}"
        self.exec_exit[exec_id] = 0
        return exec_id

    def start_exec(self, exec_id: str, detach: bool = True) -> None:
        self.calls.append(["start_exec", exec_id, str(detach)])
        self.exec_ids.append(exec_id)

    def wait_exec(self, container: str, exec_id: str) -> int:
        del container
        if exec_id in self.exec_ids:
            self.exec_ids.remove(exec_id)
        return self.exec_exit.get(exec_id, 0)

    def run(
        self, cmd: list[str], *, timeout: float | None = None, env=None
    ) -> subprocess.CompletedProcess[str]:
        del timeout, env
        self.calls.append(list(cmd))
        if not cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # Adapter / idle probe commands (not docker)
        if cmd[0] != "docker":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "stop"] and len(cmd) >= 3:
            self.graph_status[cmd[2]] = "exited"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "start"] and len(cmd) >= 3:
            # Graph wait uses the same id compose/inspect returned.
            name = cmd[2]
            self.graph_status[name] = "running"
            # Also mark mcp-style keys when start uses cid-* alias.
            return subprocess.CompletedProcess(cmd, 0, stdout=name, stderr="")
        if cmd[:3] == ["docker", "network", "disconnect"] and len(cmd) >= 5:
            nets = self.connected_networks.setdefault(cmd[4], set())
            nets.discard(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["docker", "network", "connect"] and len(cmd) >= 5:
            nets = self.connected_networks.setdefault(cmd[4], set())
            nets.add(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "volume"] and "inspect" in cmd:
            volume = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{volume}|2026-01-01T00:00:00Z\n", stderr=""
            )
        if len(cmd) >= 4 and cmd[:2] == ["docker", "compose"] and "create" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "compose"] and "ps" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="cid-from-ps\n", stderr=""
            )
        if cmd[:2] == ["docker", "wait"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="integration-log\n", stderr=""
            )
        if cmd and cmd[0] == "iptables":
            # Optional host firewall — raise so reads._try_firewall_* no-ops.
            raise OSError("iptables unavailable in integration fake")
        if cmd[:2] == ["docker", "inspect"]:
            fmt = ""
            if "-f" in cmd:
                fmt = cmd[cmd.index("-f") + 1]
            name = cmd[-1]
            if "ExecIDs" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(self.exec_ids) + "\n", stderr=""
                )
            if "{{.Id}}" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"cid-{name}\n", stderr=""
                )
            if "Network" in fmt:
                nets = self.connected_networks.get(
                    name, {self.default_network}
                )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="\n".join(sorted(nets)) + "\n", stderr=""
                )
            if "Image" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="sha256:testimg\n", stderr=""
                )
            if "Status" in fmt or ".State.Status" in fmt:
                status = self.graph_status.get(name, "running")
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=status + "\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=f"cid-{name}\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _true_bin(tmp_path: Path) -> str:
    """Cross-platform stand-in for /bin/true used as adapter argv[0]."""
    if os_name := __import__("sys").platform:
        del os_name
    script = tmp_path / "probe_ok.py"
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    return str(__import__("sys").executable)


def _production_cfg(tmp_path: Path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    root = tmp_path / "root"
    root.mkdir()
    logs = tmp_path / "logs" / "runs"
    logs.mkdir(parents=True)
    raw["runtime"]["root"] = str(root)
    raw["runtime"]["db_path"] = str(tmp_path / "state.db")
    raw["runtime"]["lock_path"] = str(tmp_path / "sync.lock")
    raw["runtime"]["logs_path"] = str(logs)
    raw["runtime"]["backups_path"] = str(tmp_path / "backups")

    remote, ubd_path, branch = _seed_ubd_git(tmp_path)
    raw["databases"]["ubd"]["source"]["path"] = str(ubd_path)
    raw["databases"]["ubd"]["source"]["remote"] = str(remote)
    raw["databases"]["ubd"]["source"]["branch"] = branch
    raw["databases"]["ubd"]["source"]["workspace_subdir"] = "src"

    probe = _true_bin(tmp_path)
    for name in ("ubd", "kb", "klon_opl"):
        db = raw["databases"][name]
        compose_g = tmp_path / f"compose-graph-{name}"
        compose_b = tmp_path / f"compose-bsl-{name}"
        _write_compose(compose_g)
        _write_compose(compose_b)
        daemon = tmp_path / f"daemon-{name}.toml"
        _write_daemon_toml(daemon)
        db["services"]["graph_compose_dir"] = str(compose_g)
        db["services"]["bsl_compose_dir"] = str(compose_b)
        db["services"]["bsl_daemon_toml"] = str(daemon)
        db["services"]["graph_mcp"] = f"mcp-{name}"
        db["services"]["bsl_container"] = f"bsl-{name}"
        db["services"]["webui_network"] = "ai-stack_default"
        # Schema requires adapters.graph mapping even when graph is not required.
        db["adapters"] = {
            "idle_probe": [probe, "-c", "pass"],
            "code_index": {
                "check_bsl": [probe, "-c", "pass"],
                "check_deletion": [probe, "-c", "pass"],
            },
            "graph": {
                "check_bsl": [probe, "-c", "pass"],
                "check_xml": [probe, "-c", "pass"],
                "check_deletion": [probe, "-c", "pass"],
            },
        }

    for name in ("kb", "klon_opl"):
        inbox = tmp_path / "inbox" / name
        inbox.mkdir(parents=True)
        source = root / "sources" / name
        source.mkdir(parents=True, exist_ok=True)
        raw["databases"][name]["source"]["inbox"] = str(inbox)
        raw["databases"][name]["source"]["source_path"] = str(source)
        _make_package(
            inbox,
            name=f"ready-{name}",
            database=name,
            export_id=f"exp-{name}",
            extra_files={"src/CommonModule.bsl": b"// probe\n"},
        )

    path = tmp_path / "contour.yaml"
    path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    from contour.config import load_config

    return load_config(str(path)), logs


@pytest.fixture
def production_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg, logs = _production_cfg(tmp_path)
    docker = IntegrationDocker(logs_dir=logs)
    for name in cfg.databases:
        docker.graph_status[cfg.databases[name].services.graph_mcp] = "running"
        docker.connected_networks[cfg.databases[name].services.bsl_container] = {
            "ai-stack_default"
        }

    monkeypatch.setattr(
        "contour.reconcile._docker_for_database",
        lambda _db, base: base,
    )
    monkeypatch.setattr("contour.watch_bsl.reload_bsl_daemon", lambda _cfg: None)
    # pause_watch / restore_watch call watch_reload which would hit real docker
    # via _watch_reload_for_database → reload_bsl_daemon (already stubbed).

    paths = RuntimePaths(
        db_path=str(tmp_path / "state.db"),
        lock_path=str(tmp_path / "sync.lock"),
    )
    return cfg, docker, paths, logs, tmp_path


def test_production_reconcile_publishes_all_three(production_env):
    cfg, docker, paths, logs, tmp_path = production_env
    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=IN_WINDOW,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=docker,
        # production runner — do not override database_runner
        watch_reload=lambda: None,
        alert_send=lambda *_a, **_k: None,
    )
    assert result == "success"

    conn = init_db(paths.db_path)
    try:
        rows = {
            r["database"]: r
            for r in conn.execute(
                "SELECT database, published_revision, lifecycle_status "
                "FROM database_state"
            )
        }
        assert set(rows) >= {"ubd", "kb", "klon_opl"}
        for name in ("ubd", "kb", "klon_opl"):
            assert rows[name]["lifecycle_status"] == "ready"
            assert rows[name]["published_revision"]

        ubd_rev = rows["ubd"]["published_revision"]
        assert read_revision(Path(cfg.databases["ubd"].source.path)) == ubd_rev
        assert (Path(cfg.databases["ubd"].source.path) / "src" / "CommonModule.bsl").is_file()

        kb_pub = Path(cfg.databases["kb"].source.source_path)
        assert read_revision(kb_pub) == rows["kb"]["published_revision"]
        assert (kb_pub / "Configuration.xml").is_file() or list(kb_pub.rglob("Configuration.xml"))

        opl_jobs = conn.execute(
            "SELECT stage FROM jobs WHERE database='klon_opl'"
        ).fetchall()
        assert opl_jobs
        assert not any("graph" in str(j["stage"]) for j in opl_jobs)

        graph_jobs = conn.execute(
            "SELECT database, stage, log_path, status FROM jobs "
            "WHERE stage LIKE 'graph%'"
        ).fetchall()
        assert graph_jobs
        assert {j["database"] for j in graph_jobs} <= {"ubd", "kb"}
        for job in graph_jobs:
            assert job["status"] in {"ok", "failed"}
            if job["log_path"]:
                assert Path(job["log_path"]).parent == logs

        runs = conn.execute(
            "SELECT database, target_revision, result FROM runs"
        ).fetchall()
        by_db = {r["database"]: r for r in runs if r["result"] == "published"}
        assert set(by_db) >= {"ubd", "kb", "klon_opl"}
        for name, run in by_db.items():
            assert run["target_revision"] == rows[name]["published_revision"]
    finally:
        conn.close()


def test_production_reap_unfinished_then_publish(production_env):
    cfg, docker, paths, logs, tmp_path = production_env
    conn = init_db(paths.db_path)
    try:
        from contour.state import StateStore

        state = StateStore(conn)
        state.register_database("ubd", "git")
        run_id = state.start_run("ubd", "pending-old")
        conn.execute(
            """
            INSERT INTO jobs (database, run_id, stage, status, container_id)
            VALUES ('ubd', ?, 'graph', 'created', 'cid-stale')
            """,
            (run_id,),
        )
        conn.commit()
        docker.graph_status["cid-stale"] = "exited"
    finally:
        conn.close()

    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=IN_WINDOW,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=docker,
        watch_reload=lambda: None,
        alert_send=lambda *_a, **_k: None,
    )
    assert result == "success"

    conn = init_db(paths.db_path)
    try:
        stale = conn.execute(
            "SELECT status, exit_code FROM jobs WHERE container_id='cid-stale'"
        ).fetchone()
        assert stale["status"] == "ok"
        assert stale["exit_code"] == 0
        ubd = conn.execute(
            "SELECT published_revision, lifecycle_status FROM database_state "
            "WHERE database='ubd'"
        ).fetchone()
        assert ubd["lifecycle_status"] == "ready"
        assert ubd["published_revision"]
    finally:
        conn.close()


def test_production_one_database_failure_others_still_publish(production_env):
    cfg, docker, paths, logs, tmp_path = production_env

    # Fail only KB BSL indexer execs; UBD / klon_opl keep exit 0.
    def wait_exec_kb_fail(container: str, exec_id: str) -> int:
        if exec_id in docker.exec_ids:
            docker.exec_ids.remove(exec_id)
        if container == cfg.databases["kb"].services.bsl_container:
            return 1
        return docker.exec_exit.get(exec_id, 0)

    docker.wait_exec = wait_exec_kb_fail  # type: ignore[method-assign]

    result = reconcile(
        cfg,
        timer=False,
        measure=False,
        now=IN_WINDOW,
        paths=paths,
        lock_backend=MemoryBackend(),
        docker=docker,
        watch_reload=lambda: None,
        alert_send=lambda *_a, **_k: None,
    )
    assert result == "failed"

    conn = init_db(paths.db_path)
    try:
        rows = {
            r["database"]: r
            for r in conn.execute(
                "SELECT database, published_revision, lifecycle_status "
                "FROM database_state"
            )
        }
        assert rows["ubd"]["lifecycle_status"] == "ready"
        assert rows["ubd"]["published_revision"]
        assert rows["klon_opl"]["lifecycle_status"] == "ready"
        assert rows["klon_opl"]["published_revision"]
        assert not rows.get("kb", {}).get("published_revision")
    finally:
        conn.close()
