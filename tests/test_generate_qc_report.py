import importlib.util
import json
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


def _write_seg_eval_csv(path, rows, extra_headers=()):
    import csv as csv_mod

    headers = ["id", "QualityScore", "downsample_factor", "effective_pixel_size_um"]
    headers += list(extra_headers)
    with open(path, "w", newline="") as fh:
        w = csv_mod.DictWriter(fh, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


def test_seg_eval_section_no_csvs(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_eval"
    d.mkdir()
    html = gqr.seg_eval_section(str(d))
    assert "No segmentation-evaluation" in html


def test_seg_eval_section_surfaces_downsample_factor_column(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_eval"
    d.mkdir()
    _write_seg_eval_csv(
        d / "segmentation_metrics.csv",
        [{"id": "P001", "QualityScore": "0.8", "downsample_factor": "4",
          "effective_pixel_size_um": "1.3"}],
    )
    html = gqr.seg_eval_section(str(d))
    assert "downsample_factor" in html
    assert "effective_pixel_size_um" in html
    assert ">4<" in html


def test_seg_eval_section_reconciliation_flags_missing_patient(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_eval"
    d.mkdir()
    # P002 is expected (from the run manifest) but silently absent here, as if
    # dropped by the QC errorStrategy='ignore' gate around SEG_QUALITY_EVAL.
    _write_seg_eval_csv(
        d / "segmentation_metrics.csv",
        [{"id": "P001", "QualityScore": "0.8", "downsample_factor": "1",
          "effective_pixel_size_um": "0.65"}],
    )
    html = gqr.seg_eval_section(str(d), expected_patients={"P001", "P002"})
    assert "1/2 expected patients present" in html
    assert "P002" in html
    assert "MISSING" in html


def test_seg_eval_section_reconciliation_all_present(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_eval"
    d.mkdir()
    _write_seg_eval_csv(
        d / "segmentation_metrics.csv",
        [{"id": "P001", "QualityScore": "0.8", "downsample_factor": "1",
          "effective_pixel_size_um": "0.65"}],
    )
    html = gqr.seg_eval_section(str(d), expected_patients={"P001"})
    assert "1/1 expected patients present" in html
    assert "MISSING" not in html


def test_seg_eval_section_escapes_html_special_patient_id(tmp_path):
    """A patient/id value containing HTML-special characters (from the
    CSV or run manifest) must be escaped, never injected raw — both in the
    metrics table and in the reconciliation "MISSING" line."""
    gqr = _load()
    d = tmp_path / "seg_eval"
    d.mkdir()
    _write_seg_eval_csv(
        d / "segmentation_metrics.csv",
        [{"id": "<b>P001</b>", "QualityScore": "0.8", "downsample_factor": "1",
          "effective_pixel_size_um": "0.65"}],
    )
    html = gqr.seg_eval_section(str(d), expected_patients={"<b>P001</b>", "A & B"})
    assert "<b>P001</b>" not in html
    assert "&lt;b&gt;P001&lt;/b&gt;" in html
    assert "A & B" not in html
    assert "A &amp; B" in html
    assert "MISSING" in html


def test_html_table_escapes_headers_and_cells():
    gqr = _load()
    out = gqr._html_table(["<script>h</script>"], [["A & B"], ["<i>x</i>"]])
    assert "<script>h</script>" not in out
    assert "&lt;script&gt;h&lt;/script&gt;" in out
    assert "A & B" not in out
    assert "A &amp; B" in out
    assert "<i>x</i>" not in out
    assert "&lt;i&gt;x&lt;/i&gt;" in out


def test_html_table_extra_body_html_appends_colspan_row():
    """`extra_body_html` (used for the feature-distance parse-error row) is
    appended raw, after the normal escaped rows, inside the same tbody."""
    gqr = _load()
    out = gqr._html_table(
        ["A"], [["x"]], "<tr><td>err</td><td colspan='3'>boom</td></tr>"
    )
    assert "<td>x</td>" in out
    assert "<td colspan='3'>boom</td>" in out
    # both the normal row and the extra row live in the same tbody
    assert out.index("<tbody>") < out.index("<td>x</td>") < out.index("colspan")
    assert out.index("colspan") < out.index("</tbody>")


def test_registration_qc_section_feature_distance_parse_error_uses_colspan(tmp_path):
    """A feature-distance JSON that fails to parse must still render as a
    single full-width (colspan) row — not silently dropped, and not
    flattened into extra empty cells — with the filename and exception
    message both HTML-escaped."""
    gqr = _load()
    feat_dir = tmp_path / "feature_dist"
    feat_dir.mkdir()
    (feat_dir / "<bad>.json").write_text("{not valid json")
    valis_dir = tmp_path / "valis_summary"
    valis_dir.mkdir()
    html = gqr.registration_qc_section(tmp_path / "reg", feat_dir, valis_dir)
    assert "colspan='3'" in html
    assert "Parse error" in html
    assert "<bad>.json" not in html
    assert "&lt;bad&gt;.json" in html


def test_manifest_section_escapes_html_special_patient_id(tmp_path):
    gqr = _load()
    summary = {
        "manifest": {
            "totals": {"patients": 1, "images": 1, "channels": 1},
            "patients": {"<b>P001</b>": {"images": 1, "channels": 1}},
        }
    }
    html = gqr.manifest_section(summary)
    assert "<b>P001</b>" not in html
    assert "&lt;b&gt;P001&lt;/b&gt;" in html


def test_seg_eval_section_no_expected_source_still_shows_present_count(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_eval"
    d.mkdir()
    _write_seg_eval_csv(
        d / "segmentation_metrics.csv",
        [{"id": "P001", "QualityScore": "0.8", "downsample_factor": "1",
          "effective_pixel_size_um": "0.65"}],
    )
    html = gqr.seg_eval_section(str(d), expected_patients=None)
    assert "1 patient(s) present" in html
    assert "not available" in html.lower()


def test_end_to_end_cli_smoke(tmp_path):
    # Minimal inputs: run summary + versions only; everything else empty dirs.
    (tmp_path / "preprocess_qc").mkdir()
    (tmp_path / "registration_qc").mkdir()
    (tmp_path / "feature_dist").mkdir()
    (tmp_path / "valis_summary").mkdir()
    (tmp_path / "postprocess_qc").mkdir()
    (tmp_path / "seg_eval").mkdir()
    (tmp_path / "distance_plots").mkdir()
    (tmp_path / "seg_qc").mkdir()
    # _summary()'s manifest has exactly patient P001; a matching seg-eval row
    # exercises the main()->seg_eval_section expected_patients wiring end to end.
    _write_seg_eval_csv(
        tmp_path / "seg_eval" / "segmentation_metrics.csv",
        [{"id": "P001", "QualityScore": "0.8", "downsample_factor": "1",
          "effective_pixel_size_um": "0.325"}],
    )
    rs = _summary(tmp_path)
    v = tmp_path / "v.yml"
    v.write_text('"A:B":\n    tool: 1.0\n')
    out = tmp_path / "report.html"
    r = subprocess.run([
        sys.executable, str(SCRIPT),
        "--preprocess-qc", str(tmp_path / "preprocess_qc"),
        "--registration-qc", str(tmp_path / "registration_qc"),
        "--feature-distances", str(tmp_path / "feature_dist"),
        "--valis-summary", str(tmp_path / "valis_summary"),
        "--postprocess-qc", str(tmp_path / "postprocess_qc"),
        "--seg-eval", str(tmp_path / "seg_eval"),
        "--distance-plots", str(tmp_path / "distance_plots"),
        "--seg-qc", str(tmp_path / "seg_qc"),
        "--run-summary", str(rs),
        "--versions", str(v),
        "--output", str(out),
        "--data-dir", str(tmp_path / "data"),
    ], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    for header in ["Run Summary", "Pipeline Stages", "Sample Manifest",
                   "Preprocessing QC", "Registration QC", "Segmentation Overlays",
                   "Postprocessing QC", "Segmentation Quality (CSE)", "Software Versions"]:
        assert header in html, f"missing section: {header}"
    # Reconciliation wiring: main() passes the manifest's patient set through
    # to seg_eval_section, and P001 (the only expected patient) is present.
    assert "1/1 expected patients present" in html
    assert "downsample_factor" in html


# ── (C) feature-TRE vs cell-displacement reconciliation ─────────────────────────
# VALIS scores registration on its own SuperPoint/SuperGlue keypoints (a self-referential,
# optimistic feature-TRE); WARP_SEG_QC scores it on independently segmented cells (centroid
# displacement in µm). The report lines the two up per stage so a divergence — features say
# aligned, cells say not — is visible instead of buried. The stage→source mapping is exact:
#   rigid       feature-TRE = rigid_D (either summary; micro-rigid doesn't touch it)
#   non_rigid   feature-TRE = non_rigid_D from the PRE-micro summary
#   micro       feature-TRE = non_rigid_D from the FINAL (post-micro) summary
def _valis_csvs(tmp_path):
    d = tmp_path / "valis_summary"
    d.mkdir()
    (d / "P001_preprocessed_summary.csv").write_text(
        "from,rigid_D,non_rigid_D\nmov,2.0,0.5\n"           # final: non_rigid_D = micro TRE
    )
    (d / "P001_preprocessed_summary_premicro.csv").write_text(
        "from,rigid_D,non_rigid_D\nmov,2.0,1.0\n"           # pre-micro: non_rigid_D = non_rigid TRE
    )
    return d


def _seg_qc_json(tmp_path):
    d = tmp_path / "seg_qc"
    d.mkdir()
    (d / "P001_mov_seg_qc.json").write_text(json.dumps({
        "patient_id": "P001", "moving": "mov", "reference": "ref",
        "stages": {
            "rigid":     {"displacement_um_p50": 2.1},
            "non_rigid": {"displacement_um_p50": 1.1},
            "micro":     {"displacement_um_p50": 6.0},
        },
    }))
    return d


def test_reconcile_maps_each_stage_to_the_right_tre_source(tmp_path):
    gqr = _load()
    rows = {(r["slide"], r["stage"]): r
            for r in gqr.reconcile_rows(str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path)))}

    assert rows[("mov", "rigid")]["feature_tre_um"] == 2.0
    assert rows[("mov", "non_rigid")]["feature_tre_um"] == 1.0   # from PRE-micro summary
    assert rows[("mov", "micro")]["feature_tre_um"] == 0.5       # from FINAL summary
    assert rows[("mov", "rigid")]["cell_disp_um"] == 2.1
    assert rows[("mov", "micro")]["cell_disp_um"] == 6.0


def test_reconcile_flags_divergence_when_features_and_cells_disagree(tmp_path):
    gqr = _load()
    rows = {(r["slide"], r["stage"]): r
            for r in gqr.reconcile_rows(str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path)))}

    # rigid/non_rigid agree closely -> not divergent; micro: features say 0.5µm, cells say 6µm.
    assert rows[("mov", "rigid")]["divergent"] is False
    assert rows[("mov", "non_rigid")]["divergent"] is False
    assert rows[("mov", "micro")]["divergent"] is True


def test_reconcile_non_rigid_tre_falls_back_to_final_without_premicro_summary(tmp_path):
    # When no pre-micro summary exists (micro_reg < 2 -> register_micro never ran), the final
    # summary's non_rigid_D still IS the non-rigid TRE, so the non_rigid row must be informative
    # rather than n/a. This is the shipped-default (micro_reg=0) configuration, so getting a real
    # value here is exactly the point of the reconciliation.
    gqr = _load()
    d = tmp_path / "valis_summary"
    d.mkdir()
    (d / "P001_preprocessed_summary.csv").write_text("from,rigid_D,non_rigid_D\nmov,2.0,0.5\n")
    rows = {(r["slide"], r["stage"]): r
            for r in gqr.reconcile_rows(str(d), str(_seg_qc_json(tmp_path)))}
    # non_rigid feature-TRE falls back to the final summary's non_rigid_D (0.5); cell disp is 1.1,
    # 1.1 <= 3*0.5 so the stage corroborates (not divergent).
    assert rows[("mov", "non_rigid")]["feature_tre_um"] == 0.5
    assert rows[("mov", "non_rigid")]["divergent"] is False


def test_reconciliation_section_renders_and_marks_divergence(tmp_path):
    gqr = _load()
    html = gqr.reconciliation_section(str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path)))
    assert "Reconciliation" in html
    assert "mov" in html
