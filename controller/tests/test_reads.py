from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from contour.config import Config, load_config
from contour.db import get_meta, init_db, set_meta
from contour.reads import apply_close, open_reads, request_close, verify_closed, wait_server_idle

EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"


def _make_config(tmp_path: Path) -> Config:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    cfg_path = tmp_path / "contour.yaml"
    cfg_path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(str(cfg_path))


def _init_conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(str(tmp_path / "contour.db"))


@dataclass
class FakeReadsDocker:
    """Injectable backend: never talks to a live Engine."""

    graph_status: str = "running"
    connected_networks: set[str] = field(default_factory=lambda: {"ai-stack_default"})
    firewall_blocked_ports: set[int] = field(default_factory=set)
    established: int = 0
    probe_idle: bool = False
    idle_after: int | None = None
    probe_calls: int = 0
    calls: list[list[str]] = field(default_factory=list)
    conn: sqlite3.Connection | None = None
    meta_at_first_mutation: str | None = None

    def idle_probe(self) -> bool:
        self.probe_calls += 1
        if self.probe_idle:
            return True
        if self.idle_after is not None and self.probe_calls >= self.idle_after:
            return True
        return False

    def _record_meta_if_mutation(self, cmd: list[str]) -> None:
        if self.conn is None or self.meta_at_first_mutation is not None:
            return
        mutating = (
            cmd[:2] == ["docker", "stop"]
            or cmd[:3] == ["docker", "network", "disconnect"]
            or cmd[:3] == ["docker", "network", "connect"]
            or cmd[:2] == ["docker", "start"]
            or (cmd and cmd[0] == "iptables")
        )
        if mutating:
            self.meta_at_first_mutation = get_meta(self.conn, "reads_close_requested")

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        self._record_meta_if_mutation(cmd)

        if cmd[:2] == ["docker", "stop"] and len(cmd) >= 3:
            if cmd[2] == "bsl-indexer":
                raise AssertionError("must not docker stop the BSL container")
            self.graph_status = "exited"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd[:2] == ["docker", "start"] and len(cmd) >= 3:
            self.graph_status = "running"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd[:3] == ["docker", "network", "disconnect"] and len(cmd) >= 5:
            self.connected_networks.discard(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd[:3] == ["docker", "network", "connect"] and len(cmd) >= 5:
            self.connected_networks.add(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd[:2] == ["docker", "inspect"]:
            fmt = ""
            if "-f" in cmd:
                fmt = cmd[cmd.index("-f") + 1]
            elif "--format" in cmd:
                fmt = cmd[cmd.index("--format") + 1]
            name = cmd[-1]
            if "Network" in fmt:
                nets = "\n".join(sorted(self.connected_networks))
                return subprocess.CompletedProcess(cmd, 0, stdout=nets + "\n", stderr="")
            if name == "mcp-metadata-graph" or "Status" in fmt:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=self.graph_status + "\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd and cmd[0] == "iptables":
            port = None
            if "--dport" in cmd:
                port = int(cmd[cmd.index("--dport") + 1])
            action = None
            if "-I" in cmd or "-A" in cmd:
                action = "add"
            elif "-D" in cmd:
                action = "delete"
            elif "-C" in cmd:
                action = "check"
            if port is not None and action == "add":
                self.firewall_blocked_ports.add(port)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if port is not None and action == "delete":
                self.firewall_blocked_ports.discard(port)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if port is not None and action == "check":
                code = 0 if port in self.firewall_blocked_ports else 1
                return subprocess.CompletedProcess(cmd, code, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if cmd and cmd[0] == "ss":
            if self.established == 0:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout="ESTABLISHED\n" * self.established, stderr=""
            )

        if "daemon" in cmd and "stop" in cmd:
            raise AssertionError("must not daemon stop")

        raise AssertionError(f"unexpected command: {cmd}")


def persist_then_apply(
    conn: sqlite3.Connection, cfg: Config, docker: FakeReadsDocker
) -> list[object]:
    """Simulate reconcile ordering: persist intent, then mutate."""
    events: list[object] = []
    request_close(conn)
    events.append(("meta", get_meta(conn, "reads_close_requested")))
    apply_close(cfg, docker)
    events.append("apply")
    return events


def test_request_close_writes_meta_before_apply_close(tmp_path: Path):
    conn = _init_conn(tmp_path)
    try:
        cfg = _make_config(tmp_path)
        docker = FakeReadsDocker(conn=conn)
        events = persist_then_apply(conn, cfg, docker)
        assert events[0] == ("meta", "1")
        assert events[1] == "apply"
        assert docker.meta_at_first_mutation == "1"
        assert docker.calls
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()


def test_request_close_does_not_set_reads_closed(tmp_path: Path):
    conn = _init_conn(tmp_path)
    try:
        request_close(conn)
        assert get_meta(conn, "reads_close_requested") == "1"
        assert get_meta(conn, "reads_closed") == ""
    finally:
        conn.close()


def test_apply_close_idempotent_when_already_closed(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker()
    apply_close(cfg, docker)
    first_adds = [c for c in docker.calls if c and c[0] == "iptables" and "-I" in c]
    apply_close(cfg, docker)
    second_adds = [c for c in docker.calls if c and c[0] == "iptables" and "-I" in c]
    assert len(first_adds) == 1
    assert len(second_adds) == 1
    disconnects = [
        c for c in docker.calls if c[:3] == ["docker", "network", "disconnect"]
    ]
    assert len(disconnects) == 1


def test_open_reads_idempotent_when_already_open(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker()
    apply_close(cfg, docker)
    open_reads(cfg, docker)
    open_reads(cfg, docker)
    connects = [c for c in docker.calls if c[:3] == ["docker", "network", "connect"]]
    assert len(connects) == 1
    deletes = [c for c in docker.calls if c and c[0] == "iptables" and "-D" in c]
    assert len(deletes) >= 1
    assert 8011 not in docker.firewall_blocked_ports


def test_apply_close_stops_graph_not_bsl(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker()
    apply_close(cfg, docker)

    assert ["docker", "stop", "mcp-metadata-graph"] in docker.calls
    assert ["docker", "stop", "bsl-indexer"] not in docker.calls
    assert not any("daemon" in c and "stop" in c for c in docker.calls)

    iptables_cmds = [c for c in docker.calls if c and c[0] == "iptables"]
    assert iptables_cmds
    joined = " ".join(iptables_cmds[0])
    assert "8011" in joined
    assert "DROP" in joined or "REJECT" in joined

    assert [
        "docker",
        "network",
        "disconnect",
        "ai-stack_default",
        "bsl-indexer",
    ] in docker.calls


def test_verify_closed_true_after_apply(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker()
    apply_close(cfg, docker)
    assert verify_closed(cfg, docker) is True


def test_verify_closed_true_without_firewall(tmp_path: Path):
    """Stand contract: graph stop + network cut is enough; host DROP optional."""
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker(
        graph_status="exited",
        connected_networks={"bsl-indexer_default"},
        firewall_blocked_ports=set(),
    )
    assert verify_closed(cfg, docker) is True


def test_verify_closed_false_when_restrictions_missing(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker()
    assert verify_closed(cfg, docker) is False


def test_verify_closed_true_when_iptables_check_raises(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = CheckTrueReadsDocker(
        graph_status="exited",
        connected_networks=set(),
    )
    assert verify_closed(cfg, docker) is True


def test_apply_close_ok_when_iptables_unavailable(tmp_path: Path):
    cfg = _make_config(tmp_path)

    class NoIptables(FakeReadsDocker):
        def run(self, cmd, *, timeout=None):
            if cmd and cmd[0] == "iptables":
                raise FileNotFoundError("iptables")
            return super().run(cmd, timeout=timeout)

    docker = NoIptables()
    apply_close(cfg, docker)
    assert verify_closed(cfg, docker) is True
    assert ["docker", "stop", "mcp-metadata-graph"] in docker.calls
    assert [
        "docker",
        "network",
        "disconnect",
        "ai-stack_default",
        "bsl-indexer",
    ] in docker.calls


@dataclass
class CheckTrueReadsDocker(FakeReadsDocker):
    """Simulates SubprocessRunner(check=True): -C miss raises, not returncode 1."""

    def run(
        self, cmd: list[str], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = super().run(cmd, timeout=timeout)
        if cmd and cmd[0] == "iptables" and "-C" in cmd:
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, cmd)
        return result


def test_wait_server_idle_not_ok_when_established_zero_but_probe_busy(
    tmp_path: Path,
):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker(graph_status="exited", established=0, probe_idle=False)
    with pytest.raises(RuntimeError, match="(?i)idle|busy|probe"):
        wait_server_idle(cfg, docker, attempts=1, interval=0)


def test_wait_server_idle_ok_when_probe_idle(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker(graph_status="exited", established=0, probe_idle=True)
    wait_server_idle(cfg, docker, attempts=1, interval=0)


def test_wait_server_idle_raises_if_graph_still_running(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker(graph_status="running", established=0, probe_idle=True)
    with pytest.raises(RuntimeError, match="(?i)running"):
        wait_server_idle(cfg, docker, attempts=1, interval=0)


def test_wait_server_idle_polls_until_idle(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker(
        graph_status="exited",
        established=0,
        probe_idle=False,
        idle_after=2,
    )
    wait_server_idle(cfg, docker, attempts=5, interval=0)
    assert docker.probe_calls >= 2


def test_open_reads_starts_graph_reconnects_unblocks(tmp_path: Path):
    cfg = _make_config(tmp_path)
    docker = FakeReadsDocker()
    apply_close(cfg, docker)
    assert verify_closed(cfg, docker) is True
    open_reads(cfg, docker)
    assert ["docker", "start", "mcp-metadata-graph"] in docker.calls
    assert [
        "docker",
        "network",
        "connect",
        "ai-stack_default",
        "bsl-indexer",
    ] in docker.calls
    iptables_delete = [
        c
        for c in docker.calls
        if c and c[0] == "iptables" and "-D" in c and "8011" in c
    ]
    assert iptables_delete
    assert verify_closed(cfg, docker) is False


# --- Task 8 Block 3: DatabaseReadController --------------------------------------


@dataclass
class FlexibleReadsDocker:
    graph_status: str = "running"
    connected_networks: set[str] = field(default_factory=lambda: {"ai-stack_default"})
    calls: list[list[str]] = field(default_factory=list)
    fail_on_network_disconnect: bool = False
    published_src: Path | None = None
    bsl_count: str = "1"

    def idle_probe(self) -> bool:
        return True

    def run(self, cmd: list[str], *, timeout: float | None = None):
        del timeout
        self.calls.append(list(cmd))
        if cmd[:2] == ["docker", "stop"]:
            self.graph_status = "exited"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "start"]:
            self.graph_status = "running"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["docker", "network", "disconnect"]:
            if self.fail_on_network_disconnect:
                self.connected_networks.discard(cmd[3])
                raise RuntimeError("bsl network disconnect failed")
            self.connected_networks.discard(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["docker", "network", "connect"]:
            self.connected_networks.add(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            fmt = cmd[cmd.index("-f") + 1] if "-f" in cmd else ""
            if "Network" in fmt:
                nets = "\n".join(sorted(self.connected_networks))
                return subprocess.CompletedProcess(cmd, 0, stdout=nets + "\n", stderr="")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=self.graph_status + "\n", stderr=""
            )
        if cmd and cmd[0] == "iptables":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        joined = " ".join(cmd)
        if "mountinfo" in joined and self.published_src is not None:
            line = (
                f"1 2 8:48 {self.published_src.as_posix()} /repos/SRC "
                "rw - ext4 /dev/sdd rw\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=line, stderr="")
        if "find" in joined and "bsl" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=self.bsl_count + "\n", stderr=""
            )
        raise AssertionError(cmd)


def _seed_open_gates(tmp_path: Path) -> Path:
    """Create a non-empty code index tree for open-gate tests."""
    src = tmp_path / "gate-src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.bsl").write_text("// x", encoding="utf-8")
    db_dir = src / ".code-index"
    db_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(db_dir / "index.db"))
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    conn.execute("INSERT INTO files(path) VALUES ('a.bsl')")
    conn.commit()
    conn.close()
    return src


def test_opl_read_controller_never_touches_graph(tmp_path: Path):
    from contour.reads import DatabaseReadController, scoped_read_key

    cfg = _make_config(tmp_path)
    opl = cfg.databases["klon_opl"]
    toml = tmp_path / "opl-daemon.toml"
    toml.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    from dataclasses import replace

    opl = replace(
        opl,
        services=replace(opl.services, bsl_daemon_toml=str(toml)),
    )
    conn = _init_conn(tmp_path)
    docker = FlexibleReadsDocker()
    ctrl = DatabaseReadController(opl, conn, docker)
    ctrl.close()
    assert ctrl.is_closed() is True
    assert not any(c[:2] == ["docker", "stop"] for c in docker.calls)
    assert get_meta(
        conn, scoped_read_key("reads_closed", "klon_opl", "code_index")
    ) == "1"
    assert get_meta(conn, "reads_closed") in (None, "")


def test_partial_close_restores_previous_availability(tmp_path: Path):
    from contour.reads import DatabaseReadController

    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    toml = tmp_path / "daemon.toml"
    toml.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    from dataclasses import replace

    ubd = replace(ubd, services=replace(ubd.services, bsl_daemon_toml=str(toml)))
    conn = _init_conn(tmp_path)
    gate_src = _seed_open_gates(tmp_path)
    docker = FlexibleReadsDocker(
        fail_on_network_disconnect=True, published_src=gate_src
    )
    ctrl = DatabaseReadController(ubd, conn, docker, open_gate_src=gate_src)
    with pytest.raises(RuntimeError, match="network disconnect"):
        ctrl.close()
    # Graph may already be stopped; restore what this attempt closed when safe.
    ctrl.reopen_if_safe_after_pre_activation_failure()
    assert docker.graph_status == "running"
    assert ubd.services.webui_network in docker.connected_networks


def test_prior_recovery_close_is_preserved(tmp_path: Path):
    from contour.reads import DatabaseReadController, scoped_read_key

    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    toml = tmp_path / "daemon.toml"
    toml.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    from dataclasses import replace

    ubd = replace(ubd, services=replace(ubd.services, bsl_daemon_toml=str(toml)))
    conn = _init_conn(tmp_path)
    for layer in ("code_index", "graph"):
        set_meta(conn, scoped_read_key("reads_closed", "ubd", layer), "1")
    docker = FlexibleReadsDocker(graph_status="exited", connected_networks=set())
    ctrl = DatabaseReadController(ubd, conn, docker)
    assert ctrl.is_closed() is True
    # Simulate prepare failure path: no close attempted this run.
    ctrl.reopen_if_safe_after_pre_activation_failure()
    assert ctrl.is_closed() is True
    assert docker.graph_status == "exited"


def test_graph_failure_keeps_verified_bsl_available(tmp_path: Path):
    from contour.reads import DatabaseReadController, scoped_read_key

    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    toml = tmp_path / "daemon.toml"
    toml.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    from dataclasses import replace

    ubd = replace(ubd, services=replace(ubd.services, bsl_daemon_toml=str(toml)))
    conn = _init_conn(tmp_path)
    gate_src = _seed_open_gates(tmp_path)
    docker = FlexibleReadsDocker(published_src=gate_src)
    ctrl = DatabaseReadController(ubd, conn, docker, open_gate_src=gate_src)
    ctrl.close()
    # Graph failed verification — leave graph closed, reopen verified BSL.
    ctrl.open_layer("code_index")
    assert ctrl.is_closed("code_index") is False
    assert ctrl.is_closed("graph") is True
    assert get_meta(conn, scoped_read_key("reads_closed", "ubd", "graph")) == "1"
    assert ubd.services.webui_network in docker.connected_networks


def test_open_code_index_refuses_empty_index(tmp_path: Path):
    from contour.reads import DatabaseReadController
    from contour.remount_bsl import BslMountError

    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    toml = tmp_path / "daemon.toml"
    toml.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    from dataclasses import replace

    ubd = replace(ubd, services=replace(ubd.services, bsl_daemon_toml=str(toml)))
    src = tmp_path / "empty-src"
    src.mkdir()
    (src / "a.bsl").write_text("x", encoding="utf-8")
    db_dir = src / ".code-index"
    db_dir.mkdir()
    conn_db = sqlite3.connect(str(db_dir / "index.db"))
    conn_db.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    conn_db.commit()
    conn_db.close()
    conn = _init_conn(tmp_path)
    docker = FlexibleReadsDocker(published_src=src, connected_networks=set())
    ctrl = DatabaseReadController(ubd, conn, docker, open_gate_src=src)
    ctrl.close_layer("code_index")
    with pytest.raises((BslMountError, RuntimeError), match="empty|files"):
        ctrl.open_layer("code_index")
    assert ctrl.is_closed("code_index") is True
    assert ubd.services.webui_network not in docker.connected_networks


def test_watcher_stays_paused_during_active_job(tmp_path: Path):
    from contour.reads import DatabaseReadController

    cfg = _make_config(tmp_path)
    ubd = cfg.databases["ubd"]
    toml = tmp_path / "daemon.toml"
    toml.write_text('[[paths]]\npath = "/repos/SRC"\n', encoding="utf-8")
    from dataclasses import replace

    ubd = replace(ubd, services=replace(ubd.services, bsl_daemon_toml=str(toml)))
    conn = _init_conn(tmp_path)
    docker = FlexibleReadsDocker()
    reloads: list[str] = []
    ctrl = DatabaseReadController(
        ubd,
        conn,
        docker,
        watch_reload=lambda: reloads.append("reload"),
        active_writer=True,
    )
    ctrl.close_layer("code_index")
    with pytest.raises(RuntimeError, match="active writer"):
        ctrl.open_layer("code_index")
    assert "path = \"/repos/SRC\"" not in toml.read_text(encoding="utf-8")
