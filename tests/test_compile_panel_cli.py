import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "compile_panel", Path(__file__).resolve().parent.parent / "bin" / "compile_panel.py")
cp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cp)


def test_compile_golden_config_has_no_errors():
    cfg, errs, warns = cp.compile_panel(
        "tests/testdata/panel_min.yaml", alpha_target=0.05, min_calibration=50, max_enumerate=100000)
    assert errs == []
    assert cfg["runtime"]["min_calibration"] == 50
    assert any(c["rate"] in ("rare", "soft") for c in cfg["constraints"]["enforce"] + cfg["constraints"]["audit"])

def test_cli_writes_config_and_report(tmp_path):
    out = tmp_path / "model_config.json"
    rep = tmp_path / "spec_report.html"
    rc = cp.main(["--panel", "tests/testdata/panel_min.yaml", "--out", str(out),
                  "--report", str(rep), "--validate-only", "--accept-all"])
    assert rc == 0
    cfg = json.loads(out.read_text())
    assert cfg["spec_version"].startswith("panel@sha256:")
    assert cfg["compiled_at"] is not None
    assert rep.exists()

def test_cli_nonzero_on_validation_error(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("markers:\n  A: {role: lineage, compartment: cell}\n"
                   "phenotypes:\n  P: {A: '+'}\n  Q: {A: '+'}\n"  # collision
                   "constraints: {}\n")
    rc = cp.main(["--panel", str(bad), "--out", str(tmp_path / "c.json"),
                  "--report", str(tmp_path / "r.html"), "--validate-only", "--accept-all"])
    assert rc != 0
