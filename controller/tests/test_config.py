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
