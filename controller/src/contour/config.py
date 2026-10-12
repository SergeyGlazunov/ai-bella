from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FORBIDDEN_FEATURES = frozenset({"kb", "mdm"})


@dataclass(frozen=True)
class RepoConfig:
    path: str
    remote: str
    branch: str
    workspace_subdir: str


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
class Config:
    repo: RepoConfig
    schedule: ScheduleConfig
    alert: AlertConfig
    services: ServicesConfig
    storage: StorageConfig
    features: FeaturesConfig | None = None
    adapters: AdaptersConfig | None = None


def _require_mapping(raw: dict[str, Any], section: str) -> dict[str, Any]:
    value = raw.get(section)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid section: {section}")
    return value


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


def _parse_adapters(adapters_raw: dict[str, Any]) -> AdaptersConfig:
    idle = _parse_command_list(adapters_raw.get("idle_probe"), "adapters.idle_probe")
    return AdaptersConfig(
        code_index=_parse_adapter_commands(adapters_raw.get("code_index"), "code_index"),
        graph=_parse_adapter_commands(adapters_raw.get("graph"), "graph"),
        idle_probe=idle,
    )


def load_config(path: str) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")

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

    return Config(
        repo=_parse_repo(_require_mapping(raw, "repo")),
        schedule=_parse_schedule(_require_mapping(raw, "schedule")),
        alert=_parse_alert(_require_mapping(raw, "alert")),
        services=_parse_services(_require_mapping(raw, "services")),
        storage=_parse_storage(_require_mapping(raw, "storage")),
        features=_parse_features(features_raw) if isinstance(features_raw, dict) else None,
        adapters=adapters,
    )
