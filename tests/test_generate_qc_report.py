import importlib.util
import json
import re
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


def _summary(tmp_path):
    p = tmp_path / "run_summary.json"
    p.write_text(
        json.dumps(
            {
                "pipeline": {"name": "mirage", "version": "0.1.0"},
                "run": {
                    "timestamp": "2026-07-24 10:00:00 UTC",
                    "mode": "standard",
                    "start": "preprocessing",
                    "stop": "postprocessing",
                },
                "params": {
                    "registration_method": "valis",
                    "seg_method": "cellsam",
                    "pixel_size": 0.325,
                },
                "manifest": {
                    "totals": {"patients": 1, "images": 3, "channels": 5},
                    "patients": {"P001": {"images": 3, "channels": 5}},
                },
            }
        )
    )
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
    html = gqr.status_strip_section(
        {"Preprocessing": True, "Registration": True, "Segmentation & Quant": False}
    )
    assert "Preprocessing" in html and "Registration" in html
    assert "Segmentation" in html


def _two_slide_seg_qc(tmp_path):
    d = tmp_path / "seg_qc"
    d.mkdir()
    for slide, rigid, nonrigid in (("movA", 6.0, 2.0), ("movB", 5.0, 1.5)):
        (d / f"{slide}_seg_qc.json").write_text(
            json.dumps(
                {
                    "moving": slide,
                    "stages": {
                        "rigid": {"displacement_um_p50": rigid},
                        "non_rigid": {"displacement_um_p50": nonrigid},
                    },
                }
            )
        )
    return d


def test_seg_qc_stage_plots_render_one_plot_per_stage(tmp_path):
    gqr = _load()
    out = gqr._seg_qc_stage_plots(str(_two_slide_seg_qc(tmp_path)))
    assert out.count("<svg") == 2
    assert "rigid — cell displacement p50" in out
    assert "non_rigid — cell displacement p50" in out
    assert "(n=2)" in out  # two slides contributed to each stage
    assert "2 slide(s)" in out


def test_seg_qc_stage_plots_order_stages_canonically_not_by_file_order(tmp_path):
    """Report diffs are read across runs; dict-insertion order is not stable."""
    gqr = _load()
    d = tmp_path / "seg_qc"
    d.mkdir()
    (d / "m_seg_qc.json").write_text(
        json.dumps(
            {
                "moving": "m",
                "stages": {
                    "micro": {"displacement_um_p50": 0.8},
                    "rigid": {"displacement_um_p50": 6.0},
                    "non_rigid": {"displacement_um_p50": 1.9},
                },
            }
        )
    )
    out = gqr._seg_qc_stage_plots(str(d))
    assert out.index("rigid —") < out.index("non_rigid —") < out.index("micro —")


def test_seg_qc_stage_plots_fall_back_to_px_when_no_pixel_size(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_qc"
    d.mkdir()
    (d / "m_seg_qc.json").write_text(
        json.dumps({"moving": "m", "stages": {"rigid": {"displacement_px_p50": 4.1}}})
    )
    out = gqr._seg_qc_stage_plots(str(d))
    assert "<svg" in out
    assert "(n=1)" in out


def test_seg_qc_stage_plots_on_an_empty_directory_is_a_notice(tmp_path):
    gqr = _load()
    d = tmp_path / "seg_qc"
    d.mkdir()
    out = gqr._seg_qc_stage_plots(str(d))
    assert "<svg" not in out
    assert 'class="empty-notice"' in out


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
    """`extra_body_html` (used for the per-file parse-error row) is
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


def test_end_to_end_cli_smoke(tmp_path):
    # Minimal inputs: run summary + versions only; everything else empty dirs.
    (tmp_path / "preprocess_qc").mkdir()
    (tmp_path / "registration_qc").mkdir()
    (tmp_path / "registration_tre").mkdir()
    (tmp_path / "postprocess_qc").mkdir()
    (tmp_path / "seg_qc").mkdir()
    rs = _summary(tmp_path)
    v = tmp_path / "v.yml"
    v.write_text('"A:B":\n    tool: 1.0\n')
    out = tmp_path / "report.html"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--preprocess-qc",
            str(tmp_path / "preprocess_qc"),
            "--registration-qc",
            str(tmp_path / "registration_qc"),
            "--registration-tre",
            str(tmp_path / "registration_tre"),
            "--postprocess-qc",
            str(tmp_path / "postprocess_qc"),
            "--seg-qc",
            str(tmp_path / "seg_qc"),
            "--run-summary",
            str(rs),
            "--versions",
            str(v),
            "--output",
            str(out),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    html = out.read_text()
    for header in [
        "Run Summary",
        "Pipeline Stages",
        "Preprocessing QC",
        "Registration QC",
        "Segmentation Overlays",
        "Postprocessing QC",
    ]:
        assert header in html, f"missing section: {header}"


# ── characterisation: the report's section list ────────────────────────────────
# Removing a section must be a DELIBERATE edit to the list below, not a silent
# consequence of deleting a function. Before this test existed the only check on
# the section set was `test_end_to_end_cli_smoke`'s `in html` loop, which is
# satisfied by a section that is present and says nothing about one that is gone
# for the wrong reason, about their ORDER, or about a section appearing twice.
_SECTION_H2 = re.compile(r"<h2>(.*?)</h2>", re.S)


def _section_titles(report_html):
    """Every <h2> the report rendered, in document order."""
    return [m.group(1).strip() for m in _SECTION_H2.finditer(report_html)]


def test_report_section_headings_are_exactly_these(tmp_path):
    for sub in (
        "preprocess_qc",
        "registration_qc",
        "registration_tre",
        "postprocess_qc",
        "seg_qc",
        "seg_residuals",
    ):
        (tmp_path / sub).mkdir()
    rs = _summary(tmp_path)
    v = tmp_path / "v.yml"
    v.write_text('"A:B":\n    tool: 1.0\n')
    out = tmp_path / "report.html"
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--preprocess-qc",
            str(tmp_path / "preprocess_qc"),
            "--registration-qc",
            str(tmp_path / "registration_qc"),
            "--registration-tre",
            str(tmp_path / "registration_tre"),
            "--postprocess-qc",
            str(tmp_path / "postprocess_qc"),
            "--seg-qc",
            str(tmp_path / "seg_qc"),
            "--seg-residuals",
            str(tmp_path / "seg_residuals"),
            "--run-summary",
            str(rs),
            "--versions",
            str(v),
            "--output",
            str(out),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert _section_titles(out.read_text()) == [
        "Run Summary",
        "Pipeline Stages",
        "Preprocessing QC",
        "Registration QC",
        "Feature-TRE vs Cell-Displacement Reconciliation",
        "Per-Cell Registration Residuals",
        "Segmentation Overlays",
        "Postprocessing QC",
    ]


# ── (C) feature-TRE vs cell-displacement reconciliation ─────────────────────────
# VALIS scores registration on its own SuperPoint/SuperGlue keypoints (a self-referential,
# optimistic feature-TRE); WARP_SEG_QC scores it on independently segmented cells (centroid
# displacement in µm). The report lines the two up per stage so a divergence — features say
# aligned, cells say not — is visible instead of buried. The stage→source mapping is exact:
#   rigid       feature-TRE = rigid_D (either summary; micro-rigid doesn't touch it)
#   non_rigid   feature-TRE = non_rigid_D from the PRE-micro summary
#   micro       feature-TRE = non_rigid_D from the FINAL (post-micro) summary
def _valis_csvs(tmp_path):
    d = tmp_path / "registration_tre"
    d.mkdir()
    (d / "P001_preprocessed_summary.csv").write_text(
        "from,rigid_D,non_rigid_D\nmov,2.0,0.5\n"  # final: non_rigid_D = micro TRE
    )
    (d / "P001_preprocessed_summary_premicro.csv").write_text(
        "from,rigid_D,non_rigid_D\nmov,2.0,1.0\n"  # pre-micro: non_rigid_D = non_rigid TRE
    )
    return d


def _seg_qc_json(tmp_path):
    d = tmp_path / "seg_qc"
    d.mkdir()
    (d / "P001_mov_seg_qc.json").write_text(
        json.dumps(
            {
                "patient_id": "P001",
                "moving": "mov",
                "reference": "ref",
                "stages": {
                    "rigid": {"displacement_um_p50": 2.1},
                    "non_rigid": {"displacement_um_p50": 1.1},
                    "micro": {"displacement_um_p50": 6.0},
                },
            }
        )
    )
    return d


def test_reconcile_maps_each_stage_to_the_right_tre_source(tmp_path):
    gqr = _load()
    rows = {
        (r["slide"], r["stage"]): r
        for r in gqr.reconcile_rows(
            str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path))
        )
    }

    assert rows[("mov", "rigid")]["feature_tre_um"] == 2.0
    assert rows[("mov", "non_rigid")]["feature_tre_um"] == 1.0  # from PRE-micro summary
    assert rows[("mov", "micro")]["feature_tre_um"] == 0.5  # from FINAL summary
    assert rows[("mov", "rigid")]["cell_disp_um"] == 2.1
    assert rows[("mov", "micro")]["cell_disp_um"] == 6.0


def test_reconcile_flags_divergence_when_features_and_cells_disagree(tmp_path):
    gqr = _load()
    rows = {
        (r["slide"], r["stage"]): r
        for r in gqr.reconcile_rows(
            str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path))
        )
    }

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
    d = tmp_path / "registration_tre"
    d.mkdir()
    (d / "P001_preprocessed_summary.csv").write_text(
        "from,rigid_D,non_rigid_D\nmov,2.0,0.5\n"
    )
    rows = {
        (r["slide"], r["stage"]): r
        for r in gqr.reconcile_rows(str(d), str(_seg_qc_json(tmp_path)))
    }
    # non_rigid feature-TRE falls back to the final summary's non_rigid_D (0.5); cell disp is 1.1,
    # 1.1 <= 3*0.5 so the stage corroborates (not divergent).
    assert rows[("mov", "non_rigid")]["feature_tre_um"] == 0.5
    assert rows[("mov", "non_rigid")]["divergent"] is False


def test_reconciliation_section_renders_and_marks_divergence(tmp_path):
    gqr = _load()
    html = gqr.reconciliation_section(
        str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path))
    )
    assert "Reconciliation" in html
    assert "mov" in html


def test_reconciliation_scatter_draws_one_circle_per_plottable_point():
    gqr = _load()
    svg = gqr._reconciliation_scatter_svg(
        [(2.0, 2.1, "mov · rigid"), (1.0, 1.1, "mov · non_rigid")], 3.0
    )
    assert svg.count("<circle") == 2
    assert "mov · rigid" in svg


def test_reconciliation_scatter_draws_two_band_edges_and_the_identity():
    gqr = _load()
    svg = gqr._reconciliation_scatter_svg([(1.0, 1.0, "a"), (10.0, 10.0, "b")], 3.0)
    assert svg.count('class="band"') == 2
    assert svg.count('class="identity"') == 1
    assert "3×" in svg


def test_reconciliation_scatter_colours_a_divergent_point_differently():
    gqr = _load()
    svg = gqr._reconciliation_scatter_svg(
        [(0.5, 6.0, "mov · micro"), (2.0, 2.1, "mov · rigid")], 3.0
    )
    # 6.0 > 3 * 0.5 -> divergent; 2.1 <= 3 * 2.0 -> agrees. The band's shaded
    # polygon ALSO fills "#27ae60" (fill-opacity 0.08, vs a point's 0.75), so a
    # bare `fill="#27ae60"` count is off by one whenever the band is drawn --
    # narrow to the point's own fill-opacity to count circles, not the band.
    assert svg.count('fill="#c0392b"') == 1
    assert svg.count('fill="#27ae60" fill-opacity="0.75"') == 1


def test_reconciliation_scatter_counts_out_points_a_log_axis_cannot_place():
    gqr = _load()
    svg = gqr._reconciliation_scatter_svg(
        [(1.0, 1.0, "a"), (0.0, 2.0, "b"), (3.0, None, "c")], 3.0
    )
    assert svg.count("<circle") == 1
    assert "2 point(s) not plottable" in svg


def test_reconciliation_scatter_labels_both_axes():
    gqr = _load()
    svg = gqr._reconciliation_scatter_svg([(1.0, 1.0, "a")], 3.0)
    assert "feature-TRE" in svg
    assert "cell displacement" in svg


def test_reconciliation_scatter_on_no_plottable_points_is_a_notice():
    gqr = _load()
    out = gqr._reconciliation_scatter_svg([], 3.0)
    assert "<svg" not in out
    assert 'class="empty-notice"' in out


def test_reconciliation_section_renders_a_scatter_not_a_table(tmp_path):
    gqr = _load()
    html_out = gqr.reconciliation_section(
        str(_valis_csvs(tmp_path)), str(_seg_qc_json(tmp_path))
    )
    assert "Reconciliation" in html_out
    assert "<svg" in html_out
    assert "<table" not in html_out
    assert "mov · micro" in html_out  # the divergent stage is labelled
    assert "1 of 3 comparable point(s)" in html_out


def _write_tre(dir_, name, coarse, rigid_p50, final_p50=None, tiles=None):
    d = {
        "moving": name,
        "coarse_tre_px": coarse,
        "n_tiles": len(tiles or []),
        "mesh_refined": final_p50 is not None,
        "rigid_tre_px": {
            "mean": rigid_p50,
            "p50": rigid_p50,
            "p90": rigid_p50,
            "max": rigid_p50,
        },
        "tiles": tiles or [],
    }
    if final_p50 is not None:
        d["residual_after_px"] = {
            "mean": final_p50,
            "p50": final_p50,
            "p90": final_p50,
            "max": final_p50,
        }
    p = dir_ / f"{name}_tre.json"
    p.write_text(json.dumps(d))
    return p


def test_tiled_tre_plots_render_headline_numbers_and_two_stage_plots(tmp_path):
    gqr = _load()
    tiles = [
        {"ix": 0, "iy": 0, "cx": 8, "cy": 8, "tre_rigid": 4.0, "tre_after": 0.4},
        {"ix": 1, "iy": 0, "cx": 24, "cy": 8, "tre_rigid": 1.0, "tre_after": 0.2},
    ]
    jp = _write_tre(
        tmp_path, "P1_DAPI", coarse=2.5, rigid_p50=4.0, final_p50=0.4, tiles=tiles
    )
    out = gqr._tiled_tre_plots([str(jp)])
    assert "P1_DAPI" in out
    assert "2.50" in out  # coarse TRE, in the caption
    assert "0.40" in out  # post-refinement final residual, in the caption
    assert "mesh refined: yes" in out
    # two stage distributions + the spatial heatmap = three <svg> elements
    assert out.count("<svg") == 3
    assert "rigid stage, per-tile TRE (px)" in out
    assert "post-refinement residual (px)" in out


def test_tiled_tre_plots_omit_the_refined_stage_when_it_was_not_measured(tmp_path):
    gqr = _load()
    jp = _write_tre(
        tmp_path,
        "P1_CD3",
        coarse=1.0,
        rigid_p50=3.0,
        final_p50=None,
        tiles=[{"ix": 0, "iy": 0, "cx": 8, "cy": 8, "tre_rigid": 3.0}],
    )
    out = gqr._tiled_tre_plots([str(jp)])
    assert "n/a (fan-out" in out  # honest n/a, not a fabricated 0
    assert "post-refinement residual" not in out
    assert out.count("<svg") == 2  # rigid distribution + heatmap


def test_tiled_tre_plots_exclude_rejected_tiles_from_the_distribution(tmp_path):
    """A rejected tile's tre_rigid is an artefact of correlating against background.

    Same rule bin/utils/tre_report.py applies to its own percentiles. Without it
    one 131.5 px reject sets the histogram's range and squashes every real tile
    into the first bin -- the exact failure the heatmap's colour scale already
    guards against.
    """
    gqr = _load()
    jp = _write_tre(
        tmp_path,
        "P1_DAPI",
        coarse=1.0,
        rigid_p50=2.0,
        final_p50=None,
        tiles=[
            {"ix": 0, "iy": 0, "cx": 8, "cy": 8, "tre_rigid": 1.0, "accepted": True},
            {"ix": 1, "iy": 0, "cx": 24, "cy": 8, "tre_rigid": 3.0, "accepted": True},
            {
                "ix": 2,
                "iy": 0,
                "cx": 40,
                "cy": 8,
                "tre_rigid": 131.5,
                "accepted": False,
            },
        ],
    )
    out = gqr._tiled_tre_plots([str(jp)])
    # the distribution saw 2 tiles, not 3
    assert "rigid stage, per-tile TRE (px) (n=2)" in out


def test_registration_section_renders_stare_tre_from_valis_dir(tmp_path):
    gqr = _load()
    valis = tmp_path / "registration_tre"
    valis.mkdir()
    _write_tre(
        valis,
        "P1_DAPI",
        coarse=2.0,
        rigid_p50=3.0,
        final_p50=0.5,
        tiles=[
            {"ix": 0, "iy": 0, "cx": 8, "cy": 8, "tre_rigid": 3.0, "tre_after": 0.5}
        ],
    )
    html = gqr.registration_qc_section(tmp_path / "reg", str(valis))
    assert "STARE Tiled TRE" in html
    assert "<svg" in html
    assert "No registration-accuracy summary found" not in html


# ── pin: VALIS CSV shape, preserved across the _read_valis_tre -> _read_intrinsic_tre rename ────
# This assertion was first written and run green against the unmodified `_read_valis_tre` (see
# task-5b-report.md for the pre-refactor pytest output). Only the callee name below was updated
# afterward, so a green result here proves the VALIS path is unchanged by the refactor rather than
# merely "whatever the new code happens to do".
def test_read_intrinsic_tre_valis_csv_shape_preserved(tmp_path):
    gqr = _load()
    out = gqr._read_intrinsic_tre(str(_valis_csvs(tmp_path)))
    assert out["mov"]["final"] == {"rigid_D": 2.0, "non_rigid_D": 0.5}
    assert out["mov"]["premicro"] == {"rigid_D": 2.0, "non_rigid_D": 1.0}


def test_read_intrinsic_tre_reads_stare_json(tmp_path):
    gqr = _load()
    d = tmp_path / "registration_tre"
    d.mkdir()
    _write_tre(d, "P1_DAPI", coarse=2.5, rigid_p50=4.0, final_p50=0.4)
    out = gqr._read_intrinsic_tre(str(d))
    assert out["P1_DAPI"]["final"] == {"rigid_D": 2.5, "non_rigid_D": 0.4}
    assert out["P1_DAPI"]["premicro"] == {}


def test_reconcile_rows_from_stare_tre_json_are_nonempty(tmp_path):
    gqr = _load()
    d = tmp_path / "registration_tre"
    d.mkdir()
    _write_tre(d, "mov", coarse=2.0, rigid_p50=3.0, final_p50=0.5)
    seg_qc = _seg_qc_json(tmp_path)  # slide "mov", matches _write_tre's moving field
    rows = {
        (r["slide"], r["stage"]): r for r in gqr.reconcile_rows(str(d), str(seg_qc))
    }
    assert rows  # non-empty: the JSON-only input still reconciles
    assert rows[("mov", "rigid")]["feature_tre_um"] == 2.0  # coarse_tre_px
    # No premicro summary for STARE -> non_rigid falls back to final's non_rigid_D (0.5),
    # same fallback path VALIS uses when micro_reg < 2.
    assert rows[("mov", "non_rigid")]["feature_tre_um"] == 0.5
    assert rows[("mov", "micro")]["feature_tre_um"] == 0.5


def test_reconciliation_section_neither_format_is_method_neutral_and_does_not_raise(
    tmp_path,
):
    gqr = _load()
    empty_tre = tmp_path / "registration_tre"
    empty_tre.mkdir()
    empty_seg_qc = tmp_path / "seg_qc"
    empty_seg_qc.mkdir()
    html = gqr.reconciliation_section(str(empty_tre), str(empty_seg_qc))
    assert "Reconciliation" in html
    assert (
        "VALIS" not in html
    )  # a tiled-only run must not see a VALIS-flavoured message
    assert "found to reconcile" in html


def test_reconcile_rows_merges_valis_csv_and_stare_json_rather_than_shadowing(tmp_path):
    gqr = _load()
    d = tmp_path / "registration_tre"
    d.mkdir()
    (d / "P001_preprocessed_summary.csv").write_text(
        "from,rigid_D,non_rigid_D\nmov_valis,2.0,0.5\n"
    )
    _write_tre(d, "mov_stare", coarse=1.5, rigid_p50=2.0, final_p50=0.3)

    seg_qc = tmp_path / "seg_qc"
    seg_qc.mkdir()
    (seg_qc / "mov_valis_seg_qc.json").write_text(
        json.dumps(
            {
                "moving": "mov_valis",
                "stages": {"rigid": {"displacement_um_p50": 2.1}},
            }
        )
    )
    (seg_qc / "mov_stare_seg_qc.json").write_text(
        json.dumps(
            {
                "moving": "mov_stare",
                "stages": {"rigid": {"displacement_um_p50": 1.6}},
            }
        )
    )

    rows = {
        (r["slide"], r["stage"]): r for r in gqr.reconcile_rows(str(d), str(seg_qc))
    }
    # Both slides are present: the CSV reader did not shadow the JSON reader, or vice versa.
    assert rows[("mov_valis", "rigid")]["feature_tre_um"] == 2.0
    assert rows[("mov_stare", "rigid")]["feature_tre_um"] == 1.5


# ── inline-SVG plotting: the pure-math seams ───────────────────────────────────
def test_finite_drops_none_nan_bool_and_strings():
    gqr = _load()
    assert gqr._finite([1, 2.5, None, float("nan"), "3", True, -4]) == [1.0, 2.5, -4.0]


def test_finite_drops_infinities():
    gqr = _load()
    assert gqr._finite([1.0, float("inf"), 2.0, float("-inf")]) == [1.0, 2.0]


def test_percentile_is_nearest_rank_and_none_on_empty():
    gqr = _load()
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert gqr._percentile(vals, 50) == 5.0
    assert gqr._percentile(vals, 90) == 9.0
    assert gqr._percentile(vals, 100) == 10.0
    assert gqr._percentile([], 50) is None
    assert gqr._percentile([None, float("nan")], 50) is None


def test_bin_counts_puts_the_maximum_in_the_last_bin():
    """The naive int((v-lo)/span*bins) sends v == hi to index `bins`, one past the end."""
    gqr = _load()
    counts, lo, hi = gqr._bin_counts([0.0, 0.5, 1.0], 2)
    assert (lo, hi) == (0.0, 1.0)
    assert counts == [1, 2]
    assert sum(counts) == 3


def test_bin_counts_widens_a_degenerate_range_so_the_single_bar_is_drawable():
    gqr = _load()
    counts, lo, hi = gqr._bin_counts([4.0, 4.0, 4.0], 5)
    assert (lo, hi) == (4.0, 5.0)
    assert sum(counts) == 3
    assert counts[0] == 3


def test_bin_counts_on_nothing_finite_is_empty_not_an_exception():
    gqr = _load()
    assert gqr._bin_counts([], 10) == ([], 0.0, 0.0)
    assert gqr._bin_counts([None, float("nan")], 10) == ([], 0.0, 0.0)
    assert gqr._bin_counts([1.0, 2.0], 0) == ([], 0.0, 0.0)


def test_bin_counts_drops_infinities_rather_than_crashing():
    """inf/-inf must not reach lo/hi -- they'd make span nan and int(nan) raise."""
    gqr = _load()
    counts, lo, hi = gqr._bin_counts([1.0, float("inf"), 2.0, float("-inf")], 5)
    assert (lo, hi) == (1.0, 2.0)
    assert sum(counts) == 2


def test_histogram_svg_draws_one_rect_per_non_empty_bin():
    gqr = _load()
    values = [0.0, 0.1, 0.2, 5.0]  # bins 0 and 9 of 10 -> two bars, eight empty
    svg = gqr._histogram_svg(values, title="t", x_label="error", bins=10)
    counts, _lo, _hi = gqr._bin_counts(values, 10)
    assert svg.count("<rect") == sum(1 for c in counts if c) == 2
    assert svg.startswith("<svg") and svg.endswith("</svg>")


def test_histogram_svg_labels_the_axes_and_states_n():
    gqr = _load()
    svg = gqr._histogram_svg([1.0, 2.0, 3.0], title="rigid TRE (px)", x_label="error")
    assert "rigid TRE (px)" in svg
    assert "(n=3)" in svg
    assert ">error<" in svg  # x-axis label
    assert ">1<" in svg and ">3<" in svg  # the lo/hi tick labels


def test_histogram_svg_draws_one_dashed_rule_per_marker():
    gqr = _load()
    svg = gqr._histogram_svg(
        [1.0, 2.0, 3.0, 4.0],
        title="t",
        x_label="error",
        rules=[(2.0, "p50"), (4.0, "p90")],
    )
    assert svg.count('class="rule"') == 2
    assert "p50" in svg and "p90" in svg


def test_histogram_svg_ignores_an_unplottable_rule_rather_than_crashing():
    gqr = _load()
    svg = gqr._histogram_svg(
        [1.0, 2.0], title="t", x_label="error", rules=[(None, "p50"), (2.0, "p90")]
    )
    assert svg.count('class="rule"') == 1
    assert "p90" in svg


def test_histogram_svg_on_empty_input_is_a_notice_not_an_svg():
    gqr = _load()
    out = gqr._histogram_svg([], title="rigid TRE", x_label="error")
    assert "<svg" not in out
    assert 'class="empty-notice"' in out
    assert "rigid TRE" in out


def test_histogram_svg_escapes_its_title_and_axis_label():
    gqr = _load()
    svg = gqr._histogram_svg([1.0], title="<b>t</b>", x_label="<i>x</i>")
    assert "<b>t</b>" not in svg and "&lt;b&gt;t&lt;/b&gt;" in svg
    assert "<i>x</i>" not in svg and "&lt;i&gt;x&lt;/i&gt;" in svg


def test_error_distribution_svg_marks_p50_and_p90():
    gqr = _load()
    values = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 9.0]
    svg = gqr._error_distribution_svg(values, title="rigid stage (px)")
    assert svg.count('class="rule"') == 2
    assert "p50" in svg and "p90" in svg
    assert "rigid stage (px)" in svg
    assert ">error<" in svg


def test_error_distribution_svg_states_n_so_a_one_slide_run_is_honest():
    gqr = _load()
    svg = gqr._error_distribution_svg([2.5], title="micro (µm)")
    assert "(n=1)" in svg
    assert "<svg" in svg


def test_error_distribution_svg_on_empty_input_is_a_notice():
    gqr = _load()
    out = gqr._error_distribution_svg([], title="non_rigid (px)")
    assert "<svg" not in out
    assert 'class="empty-notice"' in out
    assert "non_rigid (px)" in out


def test_error_distribution_svg_drops_missing_values_before_binning():
    gqr = _load()
    svg = gqr._error_distribution_svg([1.0, None, float("nan"), 3.0], title="t")
    assert "(n=2)" in svg
