from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from contour import dockerutil
from contour.db import get_meta, set_meta
from contour.dockerutil import (
    DockerRunner,
    SubprocessRunner,
    image_id,
    wait,
)

CODE_INDEX_HOME = "/data/code-index-home"
CODE_INDEX_HOME_THROWAWAY = "/tmp/contour-cih"
SRC_PATH = "/repos/SRC"

# 2026-10-28 stand incident: BSL stages ran via `docker exec` in the daemon
# container whose /repos/SRC bind pins the inode captured at container start.
# Activation rotates the published directory by rename, so the daemon (and
# every exec into it) kept seeing the pre-rotation tree: added files never
# reached the index and verify probes failed. Stages now run in throwaway
# containers with a fresh mount. The marker below invalidates attempt records
# written by the legacy exec runner so resume re-runs BSL stages once.
_RUNNER_MARKER = "#bsl-runner-v2"


def _runner(runner: DockerRunner | None) -> DockerRunner:
    return runner if runner is not None else SubprocessRunner()


def _database_for_run(cfg, conn: sqlite3.Connection, run_id: int) -> str:
    name = getattr(cfg, "name", None)
    if name:
        return str(name)
    row = conn.execute(
        "SELECT database FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is not None:
        return str(row["database"] if hasattr(row, "keys") else row[0])
    return "ubd"


def _attempt_meta_key(database: str) -> str:
    return f"bsl_attempt:{database}"


def _max_job_id(conn: sqlite3.Connection, database: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS m FROM jobs WHERE database = ?",
        (database,),
    ).fetchone()
    if row is None:
        return 0
    value = row["m"] if hasattr(row, "keys") else row[0]
    return int(value or 0)


def _record_bsl_attempt(
    conn: sqlite3.Connection,
    database: str,
    revision: str,
    storage_id: str,
    indexer_version: str,
) -> None:
    """Persist the in-flight BSL attempt (not applied_revision) as one meta blob.

    A new revision/storage/image starts a new attempt and sets job_floor to the
    current max job id so earlier successes cannot satisfy stage checks.
    Retrying the same attempt keeps the same floor so a completed background
    index from this attempt remains usable.
    """
    prev = _read_bsl_attempt(conn, database)
    if (
        prev is not None
        and prev[0] == revision
        and prev[1] == storage_id
        and prev[2] == indexer_version
    ):
        job_floor = prev[3]
    else:
        job_floor = _max_job_id(conn, database)
    payload = {
        "revision": revision,
        "storage": storage_id,
        "image": indexer_version,
        "job_floor": job_floor,
    }
    set_meta(conn, _attempt_meta_key(database), json.dumps(payload, sort_keys=True))


def _read_bsl_attempt(
    conn: sqlite3.Connection, database: str
) -> tuple[str, str, str, int] | None:
    raw = get_meta(conn, _attempt_meta_key(database))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    revision = str(data.get("revision") or "")
    storage = str(data.get("storage") or "")
    image = str(data.get("image") or "")
    try:
        job_floor = int(data.get("job_floor", -1))
    except (TypeError, ValueError):
        return None
    if not revision or not storage or not image or job_floor < 0:
        return None
    return revision, storage, image, job_floor


def _capture_live_identity(
    cfg, docker: DockerRunner
) -> tuple[str, str]:
    storage = dockerutil.volume_identity(
        cfg.storage.code_index_volume, runner=docker
    )
    image = dockerutil.image_id(cfg.services.bsl_container, runner=docker)
    if not storage or not image:
        raise RuntimeError("cannot record BSL attempt identity from live storage")
    return storage, image + _RUNNER_MARKER


def _insert_exec_job(
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    container_id: str,
    exec_id: str,
    *,
    database: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO jobs (
          database, run_id, stage, status, container_id, exec_id, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (database, run_id, stage, "created", container_id, exec_id, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def _mark_exec_running(conn: sqlite3.Connection, job_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE jobs SET status = ?, started_at = ? WHERE id = ?
        """,
        ("running", now, job_id),
    )
    conn.commit()


def _finish_exec_job(
    conn: sqlite3.Connection, job_id: int, exit_code: int, log_path: str | None = None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    status = "ok" if exit_code == 0 else "failed"
    conn.execute(
        """
        UPDATE jobs
        SET exit_code = ?, status = ?, finished_at = ?, log_path = ?
        WHERE id = ?
        """,
        (exit_code, status, now, log_path, job_id),
    )
    conn.commit()


def _published_src_host_path(cfg) -> str:
    """Host path of the published src tree (post-activation: the new tree)."""
    from pathlib import Path

    from contour.config import GitSourceConfig

    from contour.sources import published_path_for

    root = Path(published_path_for(cfg))
    source = getattr(cfg, "source", None)
    sub = ""
    if isinstance(source, GitSourceConfig):
        sub = source.workspace_subdir or ""
    path = root / sub if sub else root
    return str(path)


def _capture_container_logs(
    docker: DockerRunner, name: str, stage: str, logs_path: str | None
) -> str | None:
    """Best-effort copy of throwaway container output into the logs dir."""
    if not logs_path:
        return None
    try:
        result = docker.run(["docker", "logs", name])
        text = (getattr(result, "stdout", "") or "") + (
            getattr(result, "stderr", "") or ""
        )
        from pathlib import Path

        directory = Path(logs_path)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"contour-bsl-{stage}-{name.split('-')[-1]}.log"
        target.write_text(text, encoding="utf-8")
        return str(target)
    except Exception:
        return None


def _run_exec(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    args: list[str],
    docker: DockerRunner,
    *,
    observe_cap: float | None = None,
    on_observe=None,
    logs_path: str | None = None,
    container_runner: bool = False,
) -> None:
    """Dispatch a BSL stage.

    ``container_runner=True`` (v2 staged pipeline): run in a throwaway
    container mounting the CURRENT published tree — see
    ``_run_stage_container`` for why exec-ing the daemon is wrong there.
    ``container_runner=False`` (legacy in-place pipeline, no activation
    rotation): keep the exec-based runner; the daemon's /repos/SRC bind
    tracks the live tree because the published dir is never renamed.
    """
    if container_runner:
        _run_stage_container(
            cfg,
            conn,
            run_id,
            stage,
            args,
            docker,
            observe_cap=observe_cap,
            on_observe=on_observe,
            logs_path=logs_path,
        )
        return
    container = cfg.services.bsl_container
    from contour.dockerutil import create_exec, start_exec, wait_exec

    env = [f"CODE_INDEX_HOME={CODE_INDEX_HOME}"]
    cmd = ["bsl-indexer", *args]
    exec_id = create_exec(container, cmd, env=env, runner=docker)
    database = _database_for_run(cfg, conn, run_id)
    job_id = _insert_exec_job(
        conn, run_id, stage, container, exec_id, database=database
    )
    start_exec(exec_id, detach=True, runner=docker)
    _mark_exec_running(conn, job_id)
    exit_code = wait_exec(
        container,
        exec_id,
        runner=docker,
        observe_cap=observe_cap,
        on_observe=on_observe,
    )
    _finish_exec_job(conn, job_id, exit_code)
    if exit_code != 0:
        raise RuntimeError(f"{stage} failed with exit {exit_code}")


def _run_stage_container(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    stage: str,
    args: list[str],
    docker: DockerRunner,
    *,
    observe_cap: float | None = None,
    on_observe=None,
    logs_path: str | None = None,
) -> None:
    """Run a BSL stage in a throwaway container mounting the CURRENT tree.

    The daemon container's ``/repos/SRC`` bind pins the inode captured when
    the container started; activation rotates the published directory by
    rename, so ``docker exec`` into the daemon keeps seeing the pre-rotation
    tree (2026-10-28: added BusinessProcesses files never reached the index
    and verify probes failed on runs 68/71). A fresh container resolves the
    mount against the current directory entry, so every stage — and the
    index it builds inside the tree — always sees the activated revision.
    """
    docker_runner = docker
    image = image_id(cfg.services.bsl_container, runner=docker_runner)
    src_host = _published_src_host_path(cfg)
    name = f"contour-bsl-{stage}-{run_id}"
    create_cmd = [
        "docker",
        "create",
        "--name",
        name,
        "--network",
        "none",
        "-e",
        f"CODE_INDEX_HOME={CODE_INDEX_HOME_THROWAWAY}",
        "-v",
        f"{src_host}:{SRC_PATH}",
        image,
        "bsl-indexer",
        *args,
    ]
    docker_runner.run(create_cmd)
    database = _database_for_run(cfg, conn, run_id)
    job_id = _insert_exec_job(
        conn, run_id, stage, name, name, database=database
    )
    docker_runner.run(["docker", "start", name])
    _mark_exec_running(conn, job_id)
    exit_code = wait(
        name,
        runner=docker_runner,
        observe_cap=observe_cap,
        on_observe=on_observe,
    )
    log_path = _capture_container_logs(docker_runner, name, stage, logs_path)
    try:
        docker_runner.run(["docker", "rm", "-f", name])
    except Exception:
        pass
    _finish_exec_job(conn, job_id, exit_code, log_path)
    if exit_code != 0:
        raise RuntimeError(f"{stage} failed with exit {exit_code}")


def _run_revision(conn: sqlite3.Connection, run_id: int) -> str | None:
    row = conn.execute(
        "SELECT target_revision FROM runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    value = row["target_revision"] if hasattr(row, "keys") else row[0]
    if value in (None, ""):
        return None
    return str(value)


def _stage_succeeded_for_attempt(
    conn: sqlite3.Connection,
    database: str,
    stage: str,
    revision: str,
    job_floor: int,
) -> bool:
    """True when the latest job for this stage within the attempt finished ok.

    Jobs at or below ``job_floor`` belong to earlier attempts (other volumes /
    revisions) and must not satisfy resume.
    """
    row = conn.execute(
        """
        SELECT j.status, j.exit_code
        FROM jobs j
        JOIN runs r ON r.id = j.run_id
        WHERE j.database = ?
          AND j.stage = ?
          AND r.target_revision = ?
          AND j.id > ?
        ORDER BY j.id DESC
        LIMIT 1
        """,
        (database, stage, revision, job_floor),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"] if hasattr(row, "keys") else row[0] or "")
    exit_code = row["exit_code"] if hasattr(row, "keys") else row[1]
    try:
        code = int(exit_code) if exit_code is not None else 1
    except (TypeError, ValueError):
        return False
    return status == "ok" and code == 0


def should_resume_bsl_stages(
    cfg,
    conn: sqlite3.Connection,
    *,
    revision: str,
    runner: DockerRunner | None = None,
) -> bool:
    """Resume unfinished stages using the recorded current attempt, not applied_revision.

    An attempt records target revision + live storage/image *before* the writer
    starts. Resume requires that record to match this revision and live identity.
    Empty identity never counts as a match. applied_revision is unused here.
    """
    database = str(getattr(cfg, "name", None) or "ubd")
    row = conn.execute(
        """
        SELECT status FROM layer_state
        WHERE database = ? AND layer = 'code_index'
        """,
        (database,),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"] or "")
    if status not in {"building", "failed"}:
        return False
    attempt = _read_bsl_attempt(conn, database)
    if attempt is None:
        return False
    attempt_rev, attempt_storage, attempt_image, _job_floor = attempt
    if attempt_rev != revision:
        return False
    docker = _runner(runner)
    try:
        live_storage, live_image = _capture_live_identity(cfg, docker)
    except Exception:
        return False
    if live_storage != attempt_storage or live_image != attempt_image:
        return False
    return True


def index_bsl(
    cfg,
    conn: sqlite3.Connection,
    run_id: int,
    *,
    runner: DockerRunner | None = None,
    may_write=None,
    resume: bool | None = None,
    observe_cap: float | None = None,
    on_observe=None,
    logs_path: str | None = None,
    container_runner: bool = False,
) -> None:
    docker = _runner(runner)
    database = _database_for_run(cfg, conn, run_id)
    revision = _run_revision(conn, run_id)
    if resume is None:
        resume = bool(
            revision
            and should_resume_bsl_stages(
                cfg, conn, revision=revision, runner=docker
            )
        )
    attempt = _read_bsl_attempt(conn, database) if resume else None
    job_floor = attempt[3] if attempt is not None else -1

    def stage_ok(stage: str) -> bool:
        return bool(
            resume
            and revision is not None
            and attempt is not None
            and _stage_succeeded_for_attempt(
                conn, database, stage, revision, job_floor
            )
        )

    run_index = not stage_ok("bsl_index")
    if run_index:
        # Record attempt identity before the writer when revision is known
        # (v2). Legacy callers without target_revision still run index once.
        if revision is not None:
            live_storage, live_image = _capture_live_identity(cfg, docker)
            _record_bsl_attempt(
                conn, database, revision, live_storage, live_image
            )
        _run_exec(
            cfg, conn, run_id, "bsl_index", ["index", "--path", SRC_PATH], docker,
            observe_cap=observe_cap, on_observe=on_observe, logs_path=logs_path,
            container_runner=container_runner,
        )
    if may_write is not None:
        decision = may_write()
        if decision != "ok":
            from contour.runtime_policy import WriteWindowClosed

            raise WriteWindowClosed(str(decision))
    # Clean: attempt-scoped latest status (not historical jobs from other volumes).
    if revision is None:
        _run_exec(
            cfg, conn, run_id, "bsl_clean", ["clean", "--path", SRC_PATH], docker,
            observe_cap=observe_cap, on_observe=on_observe, logs_path=logs_path,
            container_runner=container_runner,
        )
    else:
        recorded = _read_bsl_attempt(conn, database)
        floor = recorded[3] if recorded is not None else -1
        if recorded is None or not _stage_succeeded_for_attempt(
            conn, database, "bsl_clean", revision, floor
        ):
            _run_exec(
                cfg,
                conn,
                run_id,
                "bsl_clean",
                ["clean", "--path", SRC_PATH],
                docker,
                observe_cap=observe_cap,
                on_observe=on_observe,
                logs_path=logs_path,
                container_runner=container_runner,
            )
