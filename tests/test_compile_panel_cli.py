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


def test_cli_hard_panel_error_writes_report_not_traceback(tmp_path):
    # Phenotype references a marker that was never declared -> typecheck()
    # raises PanelError, a HARD error (not the soft errors/warnings list).
    bad = tmp_path / "bad_hard.yaml"
    bad.write_text(
        "markers:\n  A: {role: lineage, compartment: cell}\n"
        "phenotypes:\n  P: {Z: '+'}\n"  # Z is undeclared
        "constraints: {}\n"
    )
    report = tmp_path / "r.html"
    rc = cp.main(["--panel", str(bad), "--out", str(tmp_path / "c.json"),
                  "--report", str(report), "--validate-only", "--accept-all"])
    assert isinstance(rc, int)
    assert rc != 0
    assert report.exists()
    assert "unknown marker" in report.read_text()


def test_compile_refuses_more_than_53_lineage_markers():
    """free_mask is a QuPath double: integers above 2**53 are not exactly
    representable, so bit 53 would be dropped silently -- no error, a wrong free set."""
    import pytest

    # `cp` was loaded from the file, so it put bin/utils on sys.path and its
    # PanelError is `phenotyping.panel_schema.PanelError`. Importing
    # `utils.phenotyping.panel_schema.PanelError` here would be a DIFFERENT class
    # object -- the dual-root import hazard panel_schema.py documents -- and
    # pytest.raises would not match it. Use the module's own symbol.
    from utils.phenotyping.panel_schema import parse_panel

    markers = {
        f"M{i}": {"role": "lineage", "compartment": "cell", "statistic": "Median"}
        for i in range(54)
    }
    panel = parse_panel({"markers": markers, "phenotypes": {}})
    with pytest.raises(Exception, match=r"2\*\*53") as exc:
        cp.guard_mask_width(panel)
    assert type(exc.value).__name__ == "PanelError"


def test_compile_accepts_exactly_53_lineage_markers():
    from utils.phenotyping.panel_schema import parse_panel

    markers = {
        f"M{i}": {"role": "lineage", "compartment": "cell", "statistic": "Median"}
        for i in range(53)
    }
    cp.guard_mask_width(parse_panel({"markers": markers, "phenotypes": {}}))
