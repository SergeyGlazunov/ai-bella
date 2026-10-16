from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

FORBIDDEN_FEATURES = frozenset({"kb", "mdm"})
VALID_LAYERS = frozenset({"graph", "code_index"})
LEGACY_RUNTIME = {
    "root": "/srv/ai",
    "db_path": "/srv/ai/state/state.db",
    "lock_path": "/srv/ai/locks/sync.lock",
    "logs_path": "/srv/ai/logs/runs",
    "backups_path": "/srv/ai/backups/state",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys (PyYAML otherwise keeps the last value)."""


def _reject_duplicate_mapping_keys(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> None:
    """Reject duplicate keys in this mapping and in unexpanded merge sources.

    ``flatten_mapping`` copies merge MappingNode pairs without constructing them,
    so inline ``<<: {a: 1, a: 2}`` would otherwise silently keep the last value.
    """
    seen: set[Any] = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            if isinstance(value_node, yaml.nodes.MappingNode):
                _reject_duplicate_mapping_keys(loader, value_node, deep)
            elif isinstance(value_node, yaml.nodes.SequenceNode):
                for subnode in value_node.value:
                    if isinstance(subnode, yaml.nodes.MappingNode):
                        _reject_duplicate_mapping_keys(loader, subnode, deep)
            continue
        if key_node.tag == "tag:yaml.org,2002:value":
            key_node.tag = "tag:yaml.org,2002:str"
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError(f"duplicate key: {key!r}")
        seen.add(key)


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    # Reject true duplicates (including inside merge templates) before expansion.
    # After flatten_mapping, merge + local override may repeat a key (local wins).
    _reject_duplicate_mapping_keys(loader, node, deep)
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class RepoConfig:
    path: str
    remote: str
    branch: str
    workspace_subdir: str


@dataclass(frozen=True)
class GitSourceConfig:
    type: Literal["git"]
    path: str
    remote: str
    branch: str
    workspace_subdir: str


@dataclass(frozen=True)
class PackageSourceConfig:
    type: Literal["package"]
    inbox: str
    source_path: str


@dataclass(frozen=True)
class ScheduleConfig:
    window_start: str
    window_end: str
    timezone: str
    build_deadline_seconds: int | None
    duration_warn_seconds: int | None = None
    measure_max_seconds: int = 43200


@dataclass(frozen=True)
class AlertConfig:
    command: list[str]


@dataclass(frozen=True)
class ServicesConfig:
    graph_compose_dir: str
    graph_project: str
    graph_service: str
    graph_mcp: str
    bsl_compose_dir: str
    bsl_project: str
    bsl_container: str
    bsl_publish_port: int
    bsl_daemon_toml: str
    webui_network: str
    # Explicit Neo4j container for probe_graph.sh — not derived from graph_volume.
    neo4j_container: str = ""


@dataclass(frozen=True)
class StorageConfig:
    graph_volume: str
    code_index_volume: str


@dataclass(frozen=True)
class FeaturesConfig:
    graph: bool = False
    code_index: bool = False
    mdd: bool = False


@dataclass(frozen=True)
class AdapterCommands:
    check_bsl: list[str] | None = None
    check_xml: list[str] | None = None
    check_deletion: list[str] | None = None


@dataclass(frozen=True)
class AdaptersConfig:
    code_index: AdapterCommands
    graph: AdapterCommands
    idle_probe: list[str]


@dataclass(frozen=True)
class RuntimeConfig:
    root: str
    db_path: str
    lock_path: str
    logs_path: str
    backups_path: str
    # Total observation budget for one writer stage (seconds). Long stages
    # (UBD graph FULL ~5.3 h) must be observed to completion in the same
    # night; running jobs are never killed. None → RuntimePolicy default (6 h).
    writer_max_observe_seconds: float | None = None
    # Refuse heavy update work when free space under runtime.root is below
    # this threshold (GiB).
    min_free_gb: float = 5.0
    # Keep this many newest accepted packages per package database (the
    # published revision's package is always kept on top of this).
    retain_packages: int = 3
    # Delete finished runs/jobs older than this many days.
    retain_runs_days: int = 90


@dataclass(frozen=True)
class DatabaseConfig:
    name: str
    source: GitSourceConfig | PackageSourceConfig
    required_layers: frozenset[str]
    services: ServicesConfig
    storage: StorageConfig
    adapters: AdaptersConfig
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    repo: RepoConfig
    schedule: ScheduleConfig
    alert: AlertConfig
    services: ServicesConfig
    storage: StorageConfig
    features: FeaturesConfig | None = None
    adapters: AdaptersConfig | None = None
    schema_version: int = 1
    runtime: RuntimeConfig | None = None
    databases: dict[str, DatabaseConfig] | None = None


def enabled_databases(cfg: Config) -> dict[str, DatabaseConfig]:
    databases = cfg.databases or {}
    return {name: db for name, db in databases.items() if db.enabled}


def _require_mapping(raw: dict[str, Any], section: str) -> dict[str, Any]:
    value = raw.get(section)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid section: {section}")
    return value


def _require_key(raw: dict[str, Any], key: str, label: str) -> Any:
    if key not in raw:
        raise ValueError(f"missing {label}")
    return raw[key]


def _validate_features(features_raw: dict[str, Any]) -> None:
    forbidden = FORBIDDEN_FEATURES.intersection(features_raw)
    if forbidden:
        key = sorted(forbidden)[0]
        raise ValueError(f"forbidden feature key: {key}")


def _validate_alert(alert_raw: dict[str, Any]) -> None:
    command = alert_raw.get("command")
    if not command:
        raise ValueError("alert.command must be a non-empty list")


def _parse_features(features_raw: dict[str, Any]) -> FeaturesConfig:
    _validate_features(features_raw)
    return FeaturesConfig(
        graph=bool(features_raw.get("graph", False)),
        code_index=bool(features_raw.get("code_index", False)),
        mdd=bool(features_raw.get("mdd", False)),
    )


def _parse_repo(repo_raw: dict[str, Any]) -> RepoConfig:
    return RepoConfig(
        path=str(repo_raw["path"]),
        remote=str(repo_raw["remote"]),
        branch=str(repo_raw["branch"]),
        workspace_subdir=str(repo_raw["workspace_subdir"]),
    )


def _optional_int(raw: dict[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"schedule.{key} must be int or null")
    return value


def _parse_schedule(schedule_raw: dict[str, Any]) -> ScheduleConfig:
    warn = _optional_int(schedule_raw, "duration_warn_seconds")
    deadline = _optional_int(schedule_raw, "build_deadline_seconds")
    if warn is None:
        warn = deadline
    measure_max = schedule_raw.get("measure_max_seconds", 43200)
    if isinstance(measure_max, bool) or not isinstance(measure_max, int):
        raise ValueError("schedule.measure_max_seconds must be int")
    window_start = str(schedule_raw["window_start"])
    window_end = str(schedule_raw["window_end"])
    timezone = str(schedule_raw["timezone"])
    if ":" not in window_start or ":" not in window_end:
        raise ValueError("schedule.window_start/window_end must be HH:MM")
    if not timezone:
        raise ValueError("schedule.timezone is required")
    return ScheduleConfig(
        window_start=window_start,
        window_end=window_end,
        timezone=timezone,
        build_deadline_seconds=deadline,
        duration_warn_seconds=warn,
        measure_max_seconds=measure_max,
    )


def _parse_alert(alert_raw: dict[str, Any]) -> AlertConfig:
    _validate_alert(alert_raw)
    command = alert_raw["command"]
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("alert.command must be a non-empty list of strings")
    return AlertConfig(command=list(command))


def _parse_services(services_raw: dict[str, Any]) -> ServicesConfig:
    neo = services_raw.get("neo4j_container", "")
    return ServicesConfig(
        graph_compose_dir=str(services_raw["graph_compose_dir"]),
        graph_project=str(services_raw["graph_project"]),
        graph_service=str(services_raw["graph_service"]),
        graph_mcp=str(services_raw["graph_mcp"]),
        bsl_compose_dir=str(services_raw["bsl_compose_dir"]),
        bsl_project=str(services_raw["bsl_project"]),
        bsl_container=str(services_raw["bsl_container"]),
        bsl_publish_port=int(services_raw["bsl_publish_port"]),
        bsl_daemon_toml=str(services_raw["bsl_daemon_toml"]),
        webui_network=str(services_raw["webui_network"]),
        neo4j_container=str(neo or "").strip(),
    )


def _parse_storage(storage_raw: dict[str, Any]) -> StorageConfig:
    return StorageConfig(
        graph_volume=str(storage_raw["graph_volume"]),
        code_index_volume=str(storage_raw["code_index_volume"]),
    )


def _parse_command_list(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item for item in raw):
        raise ValueError(f"{label} must be a non-empty list of strings")
    return list(raw)


def _parse_adapter_commands(raw: Any, section: str) -> AdapterCommands:
    if not isinstance(raw, dict):
        raise ValueError(f"adapters.{section} must be a mapping")
    check_bsl = raw.get("check_bsl")
    check_xml = raw.get("check_xml")
    check_deletion = raw.get("check_deletion")
    return AdapterCommands(
        check_bsl=_parse_command_list(check_bsl, f"adapters.{section}.check_bsl")
        if check_bsl is not None
        else None,
        check_xml=_parse_command_list(check_xml, f"adapters.{section}.check_xml")
        if check_xml is not None
        else None,
        check_deletion=_parse_command_list(
            check_deletion, f"adapters.{section}.check_deletion"
        )
        if check_deletion is not None
        else None,
    )


def _parse_adapters(adapters_raw: dict[str, Any], label: str = "adapters") -> AdaptersConfig:
    idle = _parse_command_list(adapters_raw.get("idle_probe"), f"{label}.idle_probe")
    return AdaptersConfig(
        code_index=_parse_adapter_commands(adapters_raw.get("code_index"), "code_index"),
        graph=_parse_adapter_commands(adapters_raw.get("graph"), "graph"),
        idle_probe=idle,
    )


def _positive_number(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"runtime.{key} must be a positive number")
    return float(value)


def _parse_runtime(runtime_raw: dict[str, Any]) -> RuntimeConfig:
    observe = runtime_raw.get("writer_max_observe_seconds")
    if observe is not None and (
        isinstance(observe, bool)
        or not isinstance(observe, (int, float))
        or observe <= 0
    ):
        raise ValueError(
            "runtime.writer_max_observe_seconds must be a positive number or null"
        )
    return RuntimeConfig(
        root=str(runtime_raw["root"]),
        db_path=str(runtime_raw["db_path"]),
        lock_path=str(runtime_raw["lock_path"]),
        logs_path=str(runtime_raw["logs_path"]),
        backups_path=str(runtime_raw["backups_path"]),
        writer_max_observe_seconds=(
            float(observe) if observe is not None else None
        ),
        min_free_gb=_positive_number(runtime_raw, "min_free_gb", 5.0),
        retain_packages=int(
            _positive_number(runtime_raw, "retain_packages", 3.0)
        ),
        retain_runs_days=int(
            _positive_number(runtime_raw, "retain_runs_days", 90.0)
        ),
    )


def _parse_required_layers(raw: Any, db_name: str) -> frozenset[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"databases.{db_name}.required_layers must be a non-empty list")
    layers = frozenset(str(item) for item in raw)
    unknown = layers - VALID_LAYERS
    if unknown:
        raise ValueError(
            f"databases.{db_name}.required_layers unknown: {sorted(unknown)[0]}"
        )
    return layers


def _parse_git_source(source_raw: dict[str, Any], db_name: str) -> GitSourceConfig:
    label = f"databases.{db_name}.source"
    for key in ("path", "remote", "branch", "workspace_subdir"):
        _require_key(source_raw, key, f"{label}.{key}")
    return GitSourceConfig(
        type="git",
        path=str(source_raw["path"]),
        remote=str(source_raw["remote"]),
        branch=str(source_raw["branch"]),
        workspace_subdir=str(source_raw["workspace_subdir"]),
    )


def _parse_package_source(source_raw: dict[str, Any], db_name: str) -> PackageSourceConfig:
    label = f"databases.{db_name}.source"
    for key in ("inbox", "source_path"):
        _require_key(source_raw, key, f"{label}.{key}")
    return PackageSourceConfig(
        type="package",
        inbox=str(source_raw["inbox"]),
        source_path=str(source_raw["source_path"]),
    )


def _parse_source(
    source_raw: Any, db_name: str
) -> GitSourceConfig | PackageSourceConfig:
    if not isinstance(source_raw, dict):
        raise ValueError(f"databases.{db_name}.source must be a mapping")
    source_type = source_raw.get("type")
    if source_type == "git":
        return _parse_git_source(source_raw, db_name)
    if source_type == "package":
        return _parse_package_source(source_raw, db_name)
    raise ValueError(f"databases.{db_name}.source.type must be git or package")


def _parse_database(name: str, db_raw: Any) -> DatabaseConfig:
    if not isinstance(db_raw, dict):
        raise ValueError(f"databases.{name} must be a mapping")
    if not name or not str(name).strip():
        raise ValueError("database name must be non-empty")
    adapters_raw = _require_mapping(db_raw, "adapters")
    enabled = db_raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"databases.{name}.enabled must be a boolean")
    return DatabaseConfig(
        name=str(name),
        enabled=enabled,
        source=_parse_source(db_raw.get("source"), name),
        required_layers=_parse_required_layers(db_raw.get("required_layers"), name),
        services=_parse_services(_require_mapping(db_raw, "services")),
        storage=_parse_storage(_require_mapping(db_raw, "storage")),
        adapters=_parse_adapters(adapters_raw, label=f"databases.{name}.adapters"),
    )


def _parse_databases(databases_raw: Any) -> dict[str, DatabaseConfig]:
    if not isinstance(databases_raw, dict) or not databases_raw:
        raise ValueError("databases must be a non-empty mapping")
    return {str(name): _parse_database(str(name), raw) for name, raw in databases_raw.items()}


def _repo_from_git_source(source: GitSourceConfig) -> RepoConfig:
    return RepoConfig(
        path=source.path,
        remote=source.remote,
        branch=source.branch,
        workspace_subdir=source.workspace_subdir,
    )


def _features_from_layers(layers: frozenset[str]) -> FeaturesConfig:
    return FeaturesConfig(
        graph="graph" in layers,
        code_index="code_index" in layers,
        mdd=False,
    )


def _primary_database(databases: dict[str, DatabaseConfig]) -> DatabaseConfig:
    """Pick Contour's single primary profile among enabled databases only.

    Until multi-DB orchestration lands: prefer enabled ``ubd``; otherwise require
    exactly one enabled profile (refuse ambiguous sets).
    """
    enabled = [db for db in databases.values() if db.enabled]
    if not enabled:
        raise ValueError("no enabled databases")
    by_name = {db.name: db for db in enabled}
    if "ubd" in by_name:
        return by_name["ubd"]
    if len(enabled) > 1:
        names = ", ".join(sorted(by_name))
        raise ValueError(
            "multiple enabled databases without enabled ubd "
            f"({names}); Contour currently supports a single primary profile"
        )
    return enabled[0]


def _legacy_database(
    repo: RepoConfig,
    services: ServicesConfig,
    storage: StorageConfig,
    adapters: AdaptersConfig | None,
    features: FeaturesConfig | None,
) -> DatabaseConfig:
    layers = frozenset({"graph", "code_index"})
    if features is not None:
        layers = frozenset(
            layer
            for layer, on in (
                ("graph", features.graph),
                ("code_index", features.code_index),
            )
            if on
        ) or layers
    resolved_adapters = adapters or AdaptersConfig(
        code_index=AdapterCommands(),
        graph=AdapterCommands(),
        idle_probe=["/bin/true"],
    )
    return DatabaseConfig(
        name="ubd",
        enabled=True,
        source=GitSourceConfig(
            type="git",
            path=repo.path,
            remote=repo.remote,
            branch=repo.branch,
            workspace_subdir=repo.workspace_subdir,
        ),
        required_layers=layers,
        services=services,
        storage=storage,
        adapters=resolved_adapters,
    )


def _load_v2(raw: dict[str, Any]) -> Config:
    databases = _parse_databases(raw.get("databases"))
    primary = _primary_database(databases)
    if isinstance(primary.source, GitSourceConfig):
        repo = _repo_from_git_source(primary.source)
    else:
        # Package-primary stand: keep RepoConfig shape for pre-Task-6 callers.
        repo = RepoConfig(
            path=primary.source.source_path,
            remote="",
            branch="",
            workspace_subdir="",
        )
    features_raw = raw.get("features")
    if isinstance(features_raw, dict):
        features = _parse_features(features_raw)
    else:
        features = _features_from_layers(primary.required_layers)

    # Explicit adapters: null keeps Contour.adapters unset (legacy reconcile path).
    if "adapters" in raw and raw["adapters"] is None:
        adapters = None
    elif isinstance(raw.get("adapters"), dict):
        adapters = _parse_adapters(raw["adapters"])
    else:
        adapters = primary.adapters

    return Config(
        schema_version=2,
        runtime=_parse_runtime(_require_mapping(raw, "runtime")),
        databases=databases,
        repo=repo,
        schedule=_parse_schedule(_require_mapping(raw, "schedule")),
        alert=_parse_alert(_require_mapping(raw, "alert")),
        services=primary.services,
        storage=primary.storage,
        features=features,
        adapters=adapters,
    )


def _load_legacy(raw: dict[str, Any]) -> Config:
    features_raw = raw.get("features")
    if isinstance(features_raw, dict):
        _validate_features(features_raw)

    alert_raw = raw.get("alert")
    if isinstance(alert_raw, dict):
        _validate_alert(alert_raw)

    adapters_raw = raw.get("adapters")
    adapters = (
        _parse_adapters(adapters_raw) if isinstance(adapters_raw, dict) else None
    )
    repo = _parse_repo(_require_mapping(raw, "repo"))
    services = _parse_services(_require_mapping(raw, "services"))
    storage = _parse_storage(_require_mapping(raw, "storage"))
    features = _parse_features(features_raw) if isinstance(features_raw, dict) else None
    databases = {
        "ubd": _legacy_database(repo, services, storage, adapters, features),
    }
    return Config(
        schema_version=1,
        runtime=_parse_runtime(LEGACY_RUNTIME),
        databases=databases,
        repo=repo,
        schedule=_parse_schedule(_require_mapping(raw, "schedule")),
        alert=_parse_alert(_require_mapping(raw, "alert")),
        services=services,
        storage=storage,
        features=features,
        adapters=adapters,
    )


def load_config(path: str) -> Config:
    raw = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")

    # Early rejects used by existing forbidden-feature tests (partial YAML).
    features_raw = raw.get("features")
    if isinstance(features_raw, dict):
        _validate_features(features_raw)
    alert_raw = raw.get("alert")
    if isinstance(alert_raw, dict):
        _validate_alert(alert_raw)

    version = raw.get("schema_version")
    if version == 2 or (version is None and "databases" in raw):
        return _load_v2(raw)
    if version is not None and version != 1:
        raise ValueError(f"unsupported schema_version: {version}")
    return _load_legacy(raw)
