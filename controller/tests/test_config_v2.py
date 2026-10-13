from pathlib import Path

import pytest

from contour.config import (
    GitSourceConfig,
    PackageSourceConfig,
    enabled_databases,
    load_config,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = Path(__file__).resolve().parents[2] / "contour.example.yaml"
LEGACY_MINIMAL = """\
repo:
  path: /srv/ai/ubd
  remote: "https://example.invalid/ubd.git"
  branch: main
  workspace_subdir: src
schedule:
  window_start: "01:30"
  window_end: "06:00"
  timezone: Asia/Vladivostok
  duration_warn_seconds: null
alert:
  command: ["/bin/true"]
services:
  graph_compose_dir: /srv/ai/compose/mcp_for_1c
  graph_project: mcp_for_1c
  graph_service: metadata-indexer
  graph_mcp: mcp-metadata-graph
  bsl_compose_dir: /srv/ai/compose/bsl-indexer
  bsl_project: bsl-indexer
  bsl_container: bsl-indexer
  bsl_publish_port: 8011
  bsl_daemon_toml: /srv/ai/compose/bsl-indexer/deploy/docker/daemon.toml
  webui_network: ai-stack_default
storage:
  graph_volume: mcp_for_1c_neo4j-data
  code_index_volume: bsl-indexer_bsl-indexer-home
adapters:
  idle_probe: [/opt/ai-sync/integrations/idle_bsl.sh]
  code_index:
    check_bsl: [/opt/ai-sync/integrations/probe_code_index.sh, present, "{path}"]
    check_deletion: [/opt/ai-sync/integrations/probe_code_index.sh, absent, "{path}"]
  graph:
    check_bsl: [/opt/ai-sync/integrations/probe_graph.sh, present, "{path}"]
    check_xml: [/opt/ai-sync/integrations/probe_graph.sh, present, "{path}"]
    check_deletion: [/opt/ai-sync/integrations/probe_graph.sh, absent, "{path}"]
"""


def test_v2_fixture_loads_three_profiles():
    cfg = load_config(str(FIXTURES / "config-v2.yaml"))
    assert cfg.schema_version == 2
    assert set(cfg.databases) == {"ubd", "kb", "klon_opl"}
    assert isinstance(cfg.databases["ubd"].source, GitSourceConfig)
    assert isinstance(cfg.databases["kb"].source, PackageSourceConfig)
    assert isinstance(cfg.databases["klon_opl"].source, PackageSourceConfig)
    assert cfg.databases["ubd"].required_layers == frozenset({"graph", "code_index"})
    assert cfg.databases["kb"].required_layers == frozenset({"graph", "code_index"})
    assert cfg.databases["klon_opl"].required_layers == frozenset({"code_index"})
    assert cfg.runtime.root == "/srv/ai-contour"


def test_v2_accepts_n_databases(tmp_path: Path):
    raw = (FIXTURES / "config-v2.yaml").read_text(encoding="utf-8")
    # fourth profile — schema must allow N names
    raw += """
  extra_db:
    enabled: false
    required_layers: [code_index]
    source:
      type: package
      inbox: /srv/ai-contour/inbox/extra
      source_path: /srv/ai-contour/sources/extra
    services:
      graph_compose_dir: ""
      graph_project: ""
      graph_service: ""
      graph_mcp: ""
      bsl_compose_dir: /x
      bsl_project: x
      bsl_container: x
      bsl_publish_port: 1
      bsl_daemon_toml: /x/daemon.toml
      webui_network: ai-stack_default
    storage:
      graph_volume: ""
      code_index_volume: extra-home
    adapters:
      idle_probe: [/bin/true]
      code_index:
        check_bsl: [/bin/true]
      graph:
        check_bsl: [/bin/true]
"""
    p = tmp_path / "n.yaml"
    p.write_text(raw, encoding="utf-8")
    cfg = load_config(str(p))
    assert "extra_db" in cfg.databases
    assert cfg.databases["extra_db"].enabled is False
    assert set(enabled_databases(cfg)) == {"ubd", "kb", "klon_opl"}


def test_enabled_false_excludes_from_enabled_databases(tmp_path: Path):
    text = (FIXTURES / "config-v2.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "  klon_opl:\n    enabled: true\n",
        "  klon_opl:\n    enabled: false\n",
        1,
    )
    p = tmp_path / "dis.yaml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.databases["klon_opl"].enabled is False
    assert "klon_opl" not in enabled_databases(cfg)
    assert set(enabled_databases(cfg)) == {"ubd", "kb"}


def test_disabled_ubd_not_selected_as_primary(tmp_path: Path):
    """Contour flat fields must follow an enabled profile, never disabled ubd."""
    text = (FIXTURES / "config-v2.yaml").read_text(encoding="utf-8")
    text = text.replace("  ubd:\n    enabled: true\n", "  ubd:\n    enabled: false\n", 1)
    text = text.replace(
        "  klon_opl:\n    enabled: true\n",
        "  klon_opl:\n    enabled: false\n",
        1,
    )
    p = tmp_path / "no-ubd.yaml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config(str(p))
    assert set(enabled_databases(cfg)) == {"kb"}
    assert cfg.services.bsl_container == "bsl-kb"
    assert cfg.storage.code_index_volume == "ai2-bsl-kb-home"
    assert cfg.repo.path == "/srv/ai-contour/sources/kb"


def test_disabled_ubd_with_multiple_other_enabled_raises(tmp_path: Path):
    """Until multi-DB orchestration exists, refuse ambiguous primaries."""
    text = (FIXTURES / "config-v2.yaml").read_text(encoding="utf-8")
    text = text.replace("  ubd:\n    enabled: true\n", "  ubd:\n    enabled: false\n", 1)
    p = tmp_path / "multi.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="enabled"):
        load_config(str(p))


def test_no_enabled_databases_raises(tmp_path: Path):
    text = (FIXTURES / "config-v2.yaml").read_text(encoding="utf-8")
    text = text.replace("enabled: true", "enabled: false")
    p = tmp_path / "none.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="enabled"):
        load_config(str(p))


def test_git_source_requires_fields(tmp_path: Path):
    p = tmp_path / "bad-git.yaml"
    p.write_text(
        """
schema_version: 2
runtime:
  root: /r
  db_path: /r/s.db
  lock_path: /r/l
  logs_path: /r/logs
  backups_path: /r/b
schedule:
  window_start: "01:00"
  window_end: "02:00"
  timezone: UTC
alert:
  command: [/bin/true]
databases:
  ubd:
    enabled: true
    required_layers: [graph, code_index]
    source:
      type: git
      path: /r/ubd
      remote: "https://example.invalid/x.git"
      branch: main
    services:
      graph_compose_dir: /a
      graph_project: a
      graph_service: a
      graph_mcp: a
      bsl_compose_dir: /a
      bsl_project: a
      bsl_container: a
      bsl_publish_port: 1
      bsl_daemon_toml: /a/t
      webui_network: n
    storage:
      graph_volume: g
      code_index_volume: c
    adapters:
      idle_probe: [/bin/true]
      code_index:
        check_bsl: [/bin/true]
      graph:
        check_bsl: [/bin/true]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="workspace_subdir"):
        load_config(str(p))


def test_package_source_requires_inbox(tmp_path: Path):
    p = tmp_path / "bad-pkg.yaml"
    p.write_text(
        """
schema_version: 2
runtime:
  root: /r
  db_path: /r/s.db
  lock_path: /r/l
  logs_path: /r/logs
  backups_path: /r/b
schedule:
  window_start: "01:00"
  window_end: "02:00"
  timezone: UTC
alert:
  command: [/bin/true]
databases:
  kb:
    enabled: true
    required_layers: [graph, code_index]
    source:
      type: package
      source_path: /r/kb
    services:
      graph_compose_dir: /a
      graph_project: a
      graph_service: a
      graph_mcp: a
      bsl_compose_dir: /a
      bsl_project: a
      bsl_container: a
      bsl_publish_port: 1
      bsl_daemon_toml: /a/t
      webui_network: n
    storage:
      graph_volume: g
      code_index_volume: c
    adapters:
      idle_probe: [/bin/true]
      code_index:
        check_bsl: [/bin/true]
      graph:
        check_bsl: [/bin/true]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="inbox"):
        load_config(str(p))


def test_unknown_source_type_raises(tmp_path: Path):
    p = tmp_path / "bad-type.yaml"
    p.write_text(
        """
schema_version: 2
runtime:
  root: /r
  db_path: /r/s.db
  lock_path: /r/l
  logs_path: /r/logs
  backups_path: /r/b
schedule:
  window_start: "01:00"
  window_end: "02:00"
  timezone: UTC
alert:
  command: [/bin/true]
databases:
  x:
    enabled: true
    required_layers: [code_index]
    source:
      type: svn
      path: /r/x
    services:
      graph_compose_dir: ""
      graph_project: ""
      graph_service: ""
      graph_mcp: ""
      bsl_compose_dir: /a
      bsl_project: a
      bsl_container: a
      bsl_publish_port: 1
      bsl_daemon_toml: /a/t
      webui_network: n
    storage:
      graph_volume: ""
      code_index_volume: c
    adapters:
      idle_probe: [/bin/true]
      code_index:
        check_bsl: [/bin/true]
      graph:
        check_bsl: [/bin/true]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="source.type"):
        load_config(str(p))


def test_empty_databases_raises(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text(
        """
schema_version: 2
runtime:
  root: /r
  db_path: /r/s.db
  lock_path: /r/l
  logs_path: /r/logs
  backups_path: /r/b
schedule:
  window_start: "01:00"
  window_end: "02:00"
  timezone: UTC
alert:
  command: [/bin/true]
databases: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="databases"):
        load_config(str(p))


def test_yaml_merge_key_inherits_and_overrides(tmp_path: Path):
    """UniqueKeyLoader must keep PyYAML << merge; local fields override anchors."""
    p = tmp_path / "merge.yaml"
    p.write_text(
        """
schema_version: 2
runtime:
  root: /r
  db_path: /r/s.db
  lock_path: /r/l
  logs_path: /r/logs
  backups_path: /r/b
schedule:
  window_start: "01:00"
  window_end: "02:00"
  timezone: UTC
alert:
  command: [/bin/true]
db_defaults: &db_defaults
  enabled: true
  services:
    graph_compose_dir: /a
    graph_project: a
    graph_service: a
    graph_mcp: a
    bsl_compose_dir: /a
    bsl_project: a
    bsl_container: from-defaults
    bsl_publish_port: 1
    bsl_daemon_toml: /a/t
    webui_network: n
  storage:
    graph_volume: g
    code_index_volume: c
  adapters:
    idle_probe: [/bin/true]
    code_index:
      check_bsl: [/bin/true]
    graph:
      check_bsl: [/bin/true]
databases:
  ubd:
    <<: *db_defaults
    required_layers: [graph, code_index]
    source:
      type: git
      path: /r/ubd
      remote: "https://example.invalid/ubd.git"
      branch: main
      workspace_subdir: src
    services:
      graph_compose_dir: /a
      graph_project: a
      graph_service: a
      graph_mcp: a
      bsl_compose_dir: /a
      bsl_project: a
      bsl_container: overridden
      bsl_publish_port: 1
      bsl_daemon_toml: /a/t
      webui_network: n
  kb:
    <<: *db_defaults
    enabled: false
    required_layers: [code_index]
    source:
      type: package
      inbox: /r/inbox
      source_path: /r/kb
""",
        encoding="utf-8",
    )
    cfg = load_config(str(p))
    assert cfg.databases["ubd"].enabled is True
    assert cfg.databases["ubd"].services.bsl_container == "overridden"
    assert cfg.databases["kb"].enabled is False
    assert cfg.databases["kb"].services.bsl_container == "from-defaults"
    assert set(enabled_databases(cfg)) == {"ubd"}


def test_duplicate_database_key_raises(tmp_path: Path):
    """Duplicate YAML keys under databases: must not silently overwrite."""
    p = tmp_path / "dup.yaml"
    p.write_text(
        """
schema_version: 2
runtime:
  root: /r
  db_path: /r/s.db
  lock_path: /r/l
  logs_path: /r/logs
  backups_path: /r/b
schedule:
  window_start: "01:00"
  window_end: "02:00"
  timezone: UTC
alert:
  command: [/bin/true]
databases:
  ubd:
    enabled: true
    required_layers: [graph, code_index]
    source:
      type: git
      path: /r/ubd
      remote: "https://example.invalid/ubd.git"
      branch: main
      workspace_subdir: src
    services:
      graph_compose_dir: /a
      graph_project: a
      graph_service: a
      graph_mcp: a
      bsl_compose_dir: /a
      bsl_project: a
      bsl_container: bsl-ubd
      bsl_publish_port: 1
      bsl_daemon_toml: /a/t
      webui_network: n
    storage:
      graph_volume: g
      code_index_volume: c
    adapters:
      idle_probe: [/bin/true]
      code_index:
        check_bsl: [/bin/true]
      graph:
        check_bsl: [/bin/true]
  ubd:
    enabled: true
    required_layers: [code_index]
    source:
      type: package
      inbox: /r/inbox
      source_path: /r/kb
    services:
      graph_compose_dir: ""
      graph_project: ""
      graph_service: ""
      graph_mcp: ""
      bsl_compose_dir: /b
      bsl_project: b
      bsl_container: bsl-kb
      bsl_publish_port: 2
      bsl_daemon_toml: /b/t
      webui_network: n
    storage:
      graph_volume: ""
      code_index_volume: kb-home
    adapters:
      idle_probe: [/bin/true]
      code_index:
        check_bsl: [/bin/true]
      graph:
        check_bsl: [/bin/true]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_config(str(p))


def test_legacy_normalizes_to_ubd(tmp_path: Path):
    p = tmp_path / "legacy.yaml"
    p.write_text(LEGACY_MINIMAL, encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.schema_version == 1
    assert set(cfg.databases) == {"ubd"}
    assert cfg.databases["ubd"].enabled is True
    assert isinstance(cfg.databases["ubd"].source, GitSourceConfig)
    assert cfg.databases["ubd"].source.path == "/srv/ai/ubd"
    assert cfg.repo.path == "/srv/ai/ubd"
    assert cfg.services.bsl_container == "bsl-indexer"
    assert set(enabled_databases(cfg)) == {"ubd"}


def test_example_yaml_is_v2_with_three_dbs():
    cfg = load_config(str(EXAMPLE))
    assert cfg.schema_version == 2
    assert {"ubd", "kb", "klon_opl"} <= set(cfg.databases)
    assert cfg.adapters is not None or cfg.databases["ubd"].adapters is not None
