import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "bin" / "generate_qc_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("gqr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_versions_yml_two_level(tmp_path):
    gqr = _load()
    p = tmp_path / "collated_versions.yml"
    p.write_text(
        '"MIRAGE:PREPROCESSING:CONVERT_IMAGE":\n'
        "    python: 3.10.0\n"
        '"MIRAGE:REGISTRATION:REGISTER":\n'
        "    python: 3.10.0\n"
        "    valis: 1.0.0\n"
    )
    out = gqr.parse_versions_yml(p)
    assert out["MIRAGE:PREPROCESSING:CONVERT_IMAGE"]["python"] == "3.10.0"
    assert out["MIRAGE:REGISTRATION:REGISTER"]["valis"] == "1.0.0"


def test_versions_section_renders_table(tmp_path):
    gqr = _load()
    p = tmp_path / "v.yml"
    p.write_text('"A:B":\n    tool: 1.2.3\n')
    html = gqr.versions_section(p)
    assert "Software Versions" in html
    assert "tool" in html and "1.2.3" in html


def test_versions_section_missing_file_is_graceful(tmp_path):
    gqr = _load()
    html = gqr.versions_section(tmp_path / "nope.yml")
    assert "Software Versions" in html
    assert "not available" in html.lower() or "no " in html.lower()


import json


def _summary(tmp_path):
    p = tmp_path / "run_summary.json"
    p.write_text(json.dumps({
        "pipeline": {"name": "mirage", "version": "0.1.0"},
        "run": {"timestamp": "2026-07-24 10:00:00 UTC", "mode": "standard",
                "start": "preprocessing", "stop": "postprocessing"},
        "params": {"registration_method": "valis", "seg_method": "cellsam", "pixel_size": 0.325},
        "manifest": {"totals": {"patients": 1, "images": 3, "channels": 5},
                     "patients": {"P001": {"images": 3, "channels": 5}}},
    }))
    return p


def test_parse_run_summary_missing_returns_empty(tmp_path):
    gqr = _load()
    assert gqr.parse_run_summary_json(tmp_path / "nope.json") == {}


def test_run_summary_section(tmp_path):
    gqr = _load()
    s = gqr.parse_run_summary_json(_summary(tmp_path))
    html = gqr.run_summary_section(s)
    assert "mirage" in html and "0.1.0" in html
    assert "valis" in html and "cellsam" in html
    assert "standard" in html


def test_status_strip(tmp_path):
    gqr = _load()
    html = gqr.status_strip_section({"Preprocessing": True, "Registration": True,
                                     "Segmentation & Quant": False})
    assert "Preprocessing" in html and "Registration" in html
    assert "Segmentation" in html


def test_manifest_section(tmp_path):
    gqr = _load()
    s = gqr.parse_run_summary_json(_summary(tmp_path))
    html = gqr.manifest_section(s)
    assert "P001" in html
    assert ">3<" in html or "3" in html   # image count present


def test_parse_seg_qc_json_flattens(tmp_path):
    gqr = _load()
    p = tmp_path / "P001_seg_qc.json"
    p.write_text(json.dumps({"id": "P001", "metrics": {"iou": 0.9, "n": 12}}))
    rows = gqr.parse_seg_qc_json(p)
    d = dict(rows)
    assert d["id"] == "P001"
    assert d["metrics.iou"] == "0.9"
    assert d["metrics.n"] == "12"


def test_seg_qc_section_missing(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_qc"
    d.mkdir()
    html = gqr.seg_qc_section(d)
    assert "Warp" in html or "Segmentation Warp" in html
    assert "not" in html.lower() or "no " in html.lower()


def test_seg_overlay_section_only_overlays(tmp_path):
    gqr = _load()
    d = tmp_path / "postprocess_qc"
    d.mkdir()
    # 1x1 transparent PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000154a24f9f0000000049454e44ae426082"
    )
    (d / "P001_seg_overlay.png").write_bytes(png)
    (d / "P001_intensity_distributions.png").write_bytes(png)
    html = gqr.seg_overlay_section(d)
    assert "seg_overlay" in html
    assert "intensity_distributions" not in html
