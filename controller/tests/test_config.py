from pathlib import Path

import pytest

from contour.config import load_config


def test_rejects_kb(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "features: {graph: true, code_index: true, kb: true}\n"
        "alert: {command: [/bin/true]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="kb"):
        load_config(str(p))


def test_rejects_mdm(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "features: {graph: true, code_index: true, mdm: true}\n"
        "alert: {command: [/bin/true]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mdm"):
        load_config(str(p))


def test_requires_alert_command(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "features: {graph: true, code_index: true, mdd: false}\n"
        "alert: {command: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="alert"):
        load_config(str(p))


def test_example_yaml_loads_adapters():
    example = Path(__file__).resolve().parents[2] / "contour.example.yaml"
    cfg = load_config(str(example))
    assert cfg.schema_version == 2
    assert set(cfg.databases) == {"ubd", "kb", "klon_opl"}
    ubd = cfg.databases["ubd"]
    assert ubd.adapters.idle_probe
    assert ubd.adapters.code_index.check_bsl
    assert ubd.adapters.graph.check_xml
    assert cfg.adapters is not None
    assert cfg.adapters.idle_probe
    assert cfg.schedule.duration_warn_seconds is None


def test_runtime_observe_cap_parsed_and_validated(tmp_path: Path):
    import yaml

    example = Path(__file__).resolve().parents[2] / "contour.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    raw["runtime"]["writer_max_observe_seconds"] = 600
    path = tmp_path / "c.yaml"
    path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.runtime is not None
    assert cfg.runtime.writer_max_observe_seconds == 600.0

    raw["runtime"]["writer_max_observe_seconds"] = "soon"
    path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="writer_max_observe_seconds"):
        load_config(str(path))


def test_measure_max_seconds_knob_removed(tmp_path: Path):
    """Review 1.5: the unused knob is gone; old configs still load (ignored)."""
    import yaml

    example = Path(__file__).resolve().parents[2] / "contour.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    raw["schedule"]["measure_max_seconds"] = 43200
    path = tmp_path / "c.yaml"
    path.write_text(yaml.dump(raw, sort_keys=False), encoding="utf-8")
    cfg = load_config(str(path))
    assert not hasattr(cfg.schedule, "measure_max_seconds")
