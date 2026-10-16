from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
from typing import Protocol, runtime_checkable

_RUN_FLAGS = frozenset(
    {
        "--rm",
        "--no-deps",
    }
)

_RUN_FLAGS_WITH_VALUE = frozenset(
    {
        "--entrypoint",
        "-e",
        "--env",
        "--label",
        "-l",
        "--name",
        "--user",
        "-u",
        "--volume",
        "-v",
        "--publish",
        "--workdir",
        "-w",
    }
)

_CREATE_FLAGS = frozenset(
    {
        "--build",
        "--force-recreate",
        "--no-build",
        "--no-recreate",
        "--quiet-pull",
        "--remove-orphans",
        "-V",
        "--renew-anon-volumes",
    }
)

_CREATE_FLAGS_WITH_VALUE = frozenset(
    {
        "--pull",
        "--scale",
    }
)


@runtime_checkable
class DockerRunner(Protocol):
    def run(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


class ExecRecordGone(RuntimeError):
    """Docker no longer has the exec record (collected after exit).

    The exit code is unrecoverable. Callers must resolve the job without
    claiming success (resume/rebuild path), not block as "unknown" forever.
    """


class SubprocessRunner:
    def run(
        self,
        cmd: list[str],
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=env,
        )


def _filter_create_args(args: list[str]) -> list[str]:
    filtered: list[str] = []
    service_found = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _RUN_FLAGS:
            i += 1
            continue
        if arg in _RUN_FLAGS_WITH_VALUE:
            i += 2
            continue
        if arg in _CREATE_FLAGS:
            filtered.append(arg)
            i += 1
            continue
        if arg in _CREATE_FLAGS_WITH_VALUE:
            filtered.extend([arg, args[i + 1]])
            i += 2
            continue
        if arg.startswith("-"):
            i += 2 if arg in _RUN_FLAGS_WITH_VALUE else 1
            continue
        if not service_found:
            filtered.append(arg)
            service_found = True
        i += 1
    return filtered


def _file_flags(files: list[str] | None) -> list[str]:
    flags: list[str] = []
    for path in files or ():
        flags.extend(["-f", path])
    return flags


def build_create_cmd(
    compose_dir: str,
    project: str,
    args: list[str],
    *,
    files: list[str] | None = None,
) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        compose_dir,
        "-p",
        project,
        *_file_flags(files),
        "create",
        *_filter_create_args(args),
    ]


def _service_from_create_args(args: list[str]) -> str:
    for arg in _filter_create_args(args):
        if not arg.startswith("-"):
            return arg
    raise ValueError("no service name in create args")


def build_ps_cmd(
    compose_dir: str,
    project: str,
    service: str,
    *,
    files: list[str] | None = None,
) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        compose_dir,
        "-p",
        project,
        *_file_flags(files),
        "ps",
        "--all",
        "-q",
        service,
    ]


_default_runner: DockerRunner | None = None


def _get_default_runner() -> DockerRunner:
    global _default_runner
    if _default_runner is None:
        _default_runner = SubprocessRunner()
    return _default_runner


def ping(*, runner: DockerRunner | None = None, timeout: float = 5.0) -> None:
    docker_runner = runner if runner is not None else _get_default_runner()
    docker_runner.run(["docker", "info"], timeout=timeout)


def run_readonly_with_retry(
    runner: DockerRunner,
    cmd: list[str],
    *,
    timeout: float = 30.0,
    retries: int = 2,
    pause_seconds: float = 1.0,
) -> subprocess.CompletedProcess[str]:
    """Bounded retries for transient read-only Docker inspect/info failures."""
    import time

    attempts = max(1, int(retries))
    last_exc: BaseException | None = None
    for index in range(attempts):
        try:
            return runner.run(cmd, timeout=timeout)
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
            last_exc = exc
            if index + 1 >= attempts:
                break
            if pause_seconds > 0:
                time.sleep(pause_seconds)
        except Exception as exc:
            # Non-transient command failures are not retried.
            raise
    assert last_exc is not None
    raise last_exc


def compose_create(
    compose_dir: str,
    project: str,
    args: list[str],
    *,
    runner: DockerRunner | None = None,
    files: list[str] | None = None,
    container_name: str | None = None,
) -> str:
    docker_runner = runner if runner is not None else _get_default_runner()
    create_cmd = build_create_cmd(compose_dir, project, args, files=files)
    docker_runner.run(create_cmd)
    if container_name:
        result = docker_runner.run(
            ["docker", "inspect", "-f", "{{.Id}}", container_name]
        )
        container_id = result.stdout.strip()
        if not container_id or "\n" in container_id:
            raise RuntimeError(f"no container id for {container_name!r}")
        return container_id
    service = _service_from_create_args(args)
    ps_cmd = build_ps_cmd(compose_dir, project, service, files=files)
    result = docker_runner.run(ps_cmd)
    container_id = result.stdout.strip()
    if not container_id:
        raise RuntimeError(f"no container id for service {service!r}")
    return container_id


def start(container_id: str, *, runner: DockerRunner | None = None) -> None:
    docker_runner = runner if runner is not None else _get_default_runner()
    docker_runner.run(["docker", "start", container_id])


def _run_observe_callback(on_observe) -> None:
    """Run the observation callback; a warn path must never break the wait."""
    if on_observe is None:
        return
    try:
        on_observe()
    except Exception as exc:
        print(f"[contour] observe callback failed: {exc}", file=sys.stderr)


def wait(
    container_id: str,
    *,
    runner: DockerRunner | None = None,
    timeout: float | None = None,
    observe_cap: float | None = None,
    on_observe=None,
) -> int:
    """Wait for a job container, re-arming the short chunk until the cap.

    ``timeout`` (if given) is the *total* observation budget for this call,
    preserving legacy small-budget callers. Otherwise the total comes from
    ``observe_cap`` or ``RuntimePolicy.writer_max_observe_seconds`` so a real
    nightly stage (graph FULL ≈ hours) is observed to completion in the same
    night instead of deferring after the first 300 s chunk. The container is
    never killed; total-cap exhaustion raises ``WriterStillAlive``.
    ``on_observe`` runs on every chunk expiry (≈ every 300 s) so duration
    warnings fire while the writer still runs.
    """
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY, WriterStillAlive

    docker_runner = runner if runner is not None else _get_default_runner()
    if timeout is not None:
        total = float(timeout)
    elif observe_cap is not None:
        total = float(observe_cap)
    else:
        total = DEFAULT_RUNTIME_POLICY.writer_max_observe_seconds
    chunk = DEFAULT_RUNTIME_POLICY.writer_observation_budget_seconds
    started = time.monotonic()
    while True:
        _run_observe_callback(on_observe)
        remaining = total - (time.monotonic() - started)
        if remaining <= 0:
            raise WriterStillAlive(
                f"observation budget exceeded waiting for {container_id} "
                f"({total:.0f}s)"
            )
        try:
            result = docker_runner.run(
                ["docker", "wait", container_id], timeout=min(chunk, remaining)
            )
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            if time.monotonic() - started >= total:
                raise WriterStillAlive(
                    f"observation budget exceeded waiting for {container_id} "
                    f"({total:.0f}s)"
                ) from exc
            continue
        return int(result.stdout.strip())


def list_exec_ids(container: str, *, runner: DockerRunner | None = None) -> list[str]:
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    docker_runner = runner if runner is not None else _get_default_runner()
    result = docker_runner.run(
        ["docker", "inspect", "-f", "{{json .ExecIDs}}", container],
        timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
    )
    raw = (result.stdout or "").strip() or "null"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid ExecIDs json for {container}: {raw}") from exc
    if not data:
        return []
    if not isinstance(data, list):
        raise RuntimeError(f"ExecIDs is not a list for {container}: {raw}")
    return [str(item) for item in data if item]


def _exec_inspect(exec_id: str, runner: DockerRunner) -> dict:
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    inspect_fn = getattr(runner, "inspect_exec", None)
    if callable(inspect_fn):
        payload = inspect_fn(exec_id)
        if not isinstance(payload, dict):
            raise RuntimeError(f"inspect_exec did not return a mapping for {exec_id}")
        return payload
    sock_path = "/var/run/docker.sock"
    budget = DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds

    class UnixHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(budget)
            self.sock.connect(sock_path)

    conn = UnixHTTPConnection("localhost", timeout=budget)
    try:
        conn.request("GET", f"/v1.44/exec/{exec_id}/json")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        if response.status == 404:
            raise ExecRecordGone(f"exec record gone: {exec_id}")
        if response.status >= 400:
            raise RuntimeError(f"exec inspect HTTP {response.status}: {body}")
        return json.loads(body)
    finally:
        conn.close()


def exec_state(exec_id: str, *, runner: DockerRunner | None = None) -> dict:
    """Inspect one exec instance; raises ``ExecRecordGone`` when collected."""
    docker_runner = runner if runner is not None else _get_default_runner()
    return _exec_inspect(exec_id, docker_runner)


def wait_exec(
    container: str,
    exec_id: str,
    *,
    runner: DockerRunner | None = None,
    poll_interval: float = 1.0,
    timeout: float | None = None,
    observe_cap: float | None = None,
    on_observe=None,
) -> int:
    """Wait for a detached `docker exec`. Does not `docker wait` the permanent container.

    ``timeout`` (if given) is the total observation budget (legacy callers);
    otherwise the cap comes from ``observe_cap`` or
    ``RuntimePolicy.writer_max_observe_seconds`` so long nightly BSL stages are
    observed to completion in the same night. Cap exhaustion raises
    ``WriterStillAlive`` so the caller can defer without killing the writer.
    ``ExecRecordGone`` propagates: the daemon collected the record and the
    result is unrecoverable. ``on_observe`` runs on every poll so duration
    warnings fire while the writer still runs.
    """
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY, WriterStillAlive

    docker_runner = runner if runner is not None else _get_default_runner()
    wait_fn = getattr(docker_runner, "wait_exec", None)
    if callable(wait_fn):
        return int(wait_fn(container, exec_id))
    if timeout is not None:
        budget = float(timeout)
    elif observe_cap is not None:
        budget = float(observe_cap)
    else:
        budget = DEFAULT_RUNTIME_POLICY.writer_max_observe_seconds
    deadline = time.monotonic() + budget
    while True:
        _run_observe_callback(on_observe)
        payload = _exec_inspect(exec_id, docker_runner)
        running = payload.get("Running")
        if running is False or running in {0, "false", "False"}:
            return int(payload.get("ExitCode") or 0)
        if time.monotonic() >= deadline:
            raise WriterStillAlive(
                f"observation budget exceeded waiting for exec {exec_id} "
                f"on {container} ({budget:.0f}s)"
            )
        time.sleep(poll_interval)


def volume_identity(volume: str, *, runner: DockerRunner | None = None) -> str:
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    docker_runner = runner if runner is not None else _get_default_runner()
    result = docker_runner.run(
        ["docker", "volume", "inspect", "-f", "{{.Name}}|{{.CreatedAt}}", volume],
        timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
    )
    ident = (result.stdout or "").strip()
    if not ident or ident.endswith("|") or "|" not in ident:
        raise RuntimeError(f"empty storage identity for volume {volume}")
    return ident


def image_id(container: str, *, runner: DockerRunner | None = None) -> str:
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    docker_runner = runner if runner is not None else _get_default_runner()
    result = docker_runner.run(
        ["docker", "inspect", "-f", "{{.Image}}", container],
        timeout=DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds,
    )
    image = (result.stdout or "").strip()
    if not image:
        raise RuntimeError(f"empty image id for container {container}")
    return image


def _docker_api_json(
    method: str,
    path: str,
    body: dict | None,
    runner: DockerRunner,
) -> dict:
    from contour.runtime_policy import DEFAULT_RUNTIME_POLICY

    api_fn = getattr(runner, "docker_api", None)
    if callable(api_fn):
        payload = api_fn(method, path, body)
        if not isinstance(payload, dict):
            raise RuntimeError(f"docker_api did not return a mapping for {path}")
        return payload

    sock_path = "/var/run/docker.sock"
    budget = DEFAULT_RUNTIME_POLICY.docker_command_timeout_seconds

    class UnixHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(budget)
            self.sock.connect(sock_path)

    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn = UnixHTTPConnection("localhost", timeout=budget)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        if response.status >= 400:
            raise RuntimeError(f"docker API {method} {path} HTTP {response.status}: {raw}")
        if not raw.strip():
            return {}
        return json.loads(raw)
    finally:
        conn.close()


def create_exec(
    container: str,
    cmd: list[str],
    *,
    env: list[str] | None = None,
    runner: DockerRunner | None = None,
) -> str:
    """Create a container exec via Docker API. Does not start it (R04)."""
    docker_runner = runner if runner is not None else _get_default_runner()
    create_fn = getattr(docker_runner, "create_exec", None)
    if callable(create_fn):
        exec_id = create_fn(container, cmd, env=env)
        if not exec_id:
            raise RuntimeError("create_exec returned empty id")
        return str(exec_id)
    payload = _docker_api_json(
        "POST",
        f"/v1.44/containers/{container}/exec",
        {
            "AttachStdin": False,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "Env": list(env or []),
            "Cmd": list(cmd),
        },
        docker_runner,
    )
    exec_id = payload.get("Id") or payload.get("ID")
    if not exec_id:
        raise RuntimeError(f"create exec missing Id: {payload}")
    return str(exec_id)


def start_exec(
    exec_id: str,
    *,
    detach: bool = True,
    runner: DockerRunner | None = None,
) -> None:
    """Start a previously created exec (R04)."""
    docker_runner = runner if runner is not None else _get_default_runner()
    start_fn = getattr(docker_runner, "start_exec", None)
    if callable(start_fn):
        start_fn(exec_id, detach=detach)
        return
    _docker_api_json(
        "POST",
        f"/v1.44/exec/{exec_id}/start",
        {"Detach": bool(detach), "Tty": False},
        docker_runner,
    )
