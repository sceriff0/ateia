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
