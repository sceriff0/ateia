"""Tests for bin/warp_seg_qc.py — the GeoJSON/warp segmentation-overlap registration QC
(reg_qc=2). Warps native cell polygons through the VALIS registrar and scores before/after.

The VALIS warp is injected via a fake registrar (like the benchmark's eval_seg), so the
rasterize/score logic is testable without VALIS/GPU. Needs numpy + scikit-image.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("skimage")

import warp_seg_qc


def _square(x0, y0, s):
    ring = [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]]
    return {"type": "Feature", "properties": {"label": 1},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _write(path, fc):
    path.write_text(json.dumps(fc))
    return str(path)


class _FakeSlide:
    def __init__(self, fc):
        self.fc = fc
        self.calls = []

    def warp_geojson(self, geojson_f, pt_level=0, non_rigid=True, crop="overlap"):
        self.calls.append(dict(pt_level=pt_level, non_rigid=non_rigid, crop=crop))
        return self.fc


class _FakeRegistrar:
    def __init__(self, slide_dict):
        self.slide_dict = slide_dict


# ── rasterization / scoring core ───────────────────────────────────────────────
def test_rasterize_features_labels_each_polygon():
    fc = _fc(_square(0, 0, 2), _square(6, 6, 2))
    lab = warp_seg_qc.rasterize_features(fc, (10, 10))
    assert lab[0, 0] == 1 and lab[6, 6] == 2 and lab[4, 4] == 0


def test_common_grid_covers_all_features():
    shape_rc, offset = warp_seg_qc.common_grid(_fc(_square(0, 0, 2)), _fc(_square(8, 6, 2)), pad=0)
    assert offset == (0.0, 0.0)
    assert shape_rc == (8, 10)


def test_score_identical_feature_collections_is_perfect():
    fc = _fc(_square(0, 0, 4), _square(10, 10, 4))
    out = warp_seg_qc._score(fc, fc, iou_thresh=0.5)
    assert out["dice"] == 1.0 and out["instance_f1"] == 1.0


# ── warp passthrough ────────────────────────────────────────────────────────────
def test_warp_cells_forwards_params(tmp_path):
    fc = _fc(_square(0, 0, 2))
    slide = _FakeSlide(fc)
    reg = _FakeRegistrar({"mov": slide})
    gj = _write(tmp_path / "m.geojson", fc)
    out = warp_seg_qc.warp_cells(reg, "mov", gj, pt_level=0, non_rigid=True, crop="overlap")
    assert out is fc
    assert slide.calls[0] == dict(pt_level=0, non_rigid=True, crop="overlap")


# ── before/after/delta orchestration ────────────────────────────────────────────
def test_run_reports_before_after_delta(tmp_path):
    # native geojsons are disjoint (before = 0); the warp aligns them perfectly (after = 1).
    ref_native = _fc(_square(0, 0, 4))
    mov_native = _fc(_square(20, 20, 4))         # far apart before registration
    aligned = _fc(_square(0, 0, 4))
    ref_gj = _write(tmp_path / "ref.geojson", ref_native)
    mov_gj = _write(tmp_path / "mov.geojson", mov_native)
    reg = _FakeRegistrar({"ref": _FakeSlide(aligned), "mov": _FakeSlide(aligned.copy())})
    out = warp_seg_qc.run("pickle", "ref", "mov", ref_gj, mov_gj, loader=lambda _p: reg)
    assert out["before"]["dice"] == 0.0
    assert out["after"]["dice"] == 1.0
    assert out["delta"]["dice"] == 1.0
    assert out["delta"]["instance_f1"] == out["after"]["instance_f1"] - out["before"]["instance_f1"]


def test_write_report_carries_identity_and_metrics(tmp_path):
    fc = _fc(_square(0, 0, 4))
    ref_gj = _write(tmp_path / "ref.geojson", fc)
    mov_gj = _write(tmp_path / "mov.geojson", fc)
    reg = _FakeRegistrar({"ref": _FakeSlide(fc), "mov": _FakeSlide(fc)})
    out = tmp_path / "P1_seg_qc.json"
    warp_seg_qc.write_report("pickle", "ref", "mov", ref_gj, mov_gj, str(out),
                             patient_id="P1", moving_name="P1_CD3.ome.tiff",
                             reference_name="P1_DAPI.ome.tiff", loader=lambda _p: reg)
    data = json.loads(out.read_text())
    assert data["patient_id"] == "P1"
    assert data["moving"] == "P1_CD3.ome.tiff"
    assert data["after"]["dice"] == 1.0


# ── rasterization scale: the fix for the whole-slide exit-140 kills ─────────────
def _grid_fc(n_x, n_y, pitch, size, dx=0.0, dy=0.0):
    """A regular grid of square 'cells' — a stand-in for whole-slide nuclei."""
    return _fc(*[_square(i * pitch + dx, j * pitch + dy, size)
                 for j in range(n_y) for i in range(n_x)])


def test_choose_raster_scale_respects_the_pixel_budget():
    """A whole-slide grid must be scaled down to fit, not allocated at level 0."""
    info = warp_seg_qc.choose_raster_scale((60_000, 40_000), cell_area_px=300.0,
                                           max_raster_px=400_000_000)
    assert info["scale"] < 1.0
    assert info["scaled_grid_px"] <= 400_000_000
    # 60000x40000 = 2.4e9 px; the budget is 4e8, so the linear scale is sqrt(1/6).
    assert info["scale"] == pytest.approx((400_000_000 / 2.4e9) ** 0.5, rel=1e-6)


def test_choose_raster_scale_is_a_noop_when_the_grid_already_fits():
    info = warp_seg_qc.choose_raster_scale((1000, 1000), cell_area_px=300.0,
                                           max_raster_px=400_000_000)
    assert info["scale"] == 1.0 and info["cells_resolvable"] is True
    assert info["downsampled"] is False


@pytest.mark.parametrize("cap", [0, None])
def test_no_cap_means_native_resolution(cap):
    """The default. Block-wise metrics make full-res affordable (8 B per slide px), so a
    QC number is quoted at native resolution unless a guard is explicitly set."""
    info = warp_seg_qc.choose_raster_scale((150_000, 150_000), cell_area_px=300.0,
                                           max_raster_px=cap)
    assert info["scale"] == 1.0 and info["downsampled"] is False


def test_default_is_native_resolution_end_to_end(tmp_path):
    ref = _grid_fc(4, 4, 60, 40)          # small extent: the default rasterizes it as-is
    ref_gj = _write(tmp_path / "ref.geojson", ref)
    mov_gj = _write(tmp_path / "mov.geojson", ref)
    reg = _FakeRegistrar({"ref": _FakeSlide(ref), "mov": _FakeSlide(ref)})
    out = warp_seg_qc.run("pickle", "ref", "mov", ref_gj, mov_gj, loader=lambda _p: reg)
    assert warp_seg_qc.DEFAULT_MAX_RASTER_PX == 0
    assert out["raster"]["scale"] == 1.0 and out["raster"]["downsampled"] is False


def test_choose_raster_scale_flags_unresolvable_cells_instead_of_relaxing():
    """Memory is the binding constraint: the scale must still fit the budget, and the
    loss of cell resolution is reported rather than silently traded away."""
    info = warp_seg_qc.choose_raster_scale((200_000, 200_000), cell_area_px=300.0,
                                           max_raster_px=10_000, min_cell_area_px=20.0)
    assert info["scaled_grid_px"] <= 10_000          # budget still honoured
    assert info["cells_resolvable"] is False          # and the cost is surfaced
    assert info["median_cell_area_px_at_scale"] < 20.0


def test_rasterize_at_scale_shrinks_the_grid_and_preserves_the_verdict():
    """Downsampling quantizes the area integration, not the geometry: a half-scale
    raster must reach the same conclusion about a known shift."""
    ref = _grid_fc(6, 6, 60, 40)
    mov = _grid_fc(6, 6, 60, 40, dx=8)          # 8 px shift, 40 px cells
    full = warp_seg_qc._score(ref, mov, scale=1.0)
    half = warp_seg_qc._score(ref, mov, scale=0.5)
    assert half["grid_rc"][0] < full["grid_rc"][0]
    assert half["dice"] == pytest.approx(full["dice"], abs=0.02)
    assert half["instance_f1"] == pytest.approx(full["instance_f1"], abs=0.02)


def test_run_applies_one_shared_scale_to_before_and_after(tmp_path):
    """before and after must be rasterized at the SAME scale or the delta is meaningless.
    Uses a native extent far past the budget so a real downsample is forced."""
    ref_native = _grid_fc(4, 4, 30_000, 4_000)
    mov_native = _grid_fc(4, 4, 30_000, 4_000, dx=12_000)   # badly misaligned
    aligned = _grid_fc(4, 4, 30_000, 4_000)
    ref_gj = _write(tmp_path / "ref.geojson", ref_native)
    mov_gj = _write(tmp_path / "mov.geojson", mov_native)
    reg = _FakeRegistrar({"ref": _FakeSlide(aligned), "mov": _FakeSlide(aligned)})

    out = warp_seg_qc.run("pickle", "ref", "mov", ref_gj, mov_gj, loader=lambda _p: reg,
                          max_raster_px=4_000_000)
    assert out["raster"]["scale"] < 1.0
    for side in ("before", "after"):
        h, w = out[side]["grid_rc"]
        assert h * w <= 4_000_000 * 1.05          # both grids inside the budget
    assert out["after"]["dice"] > out["before"]["dice"]
    assert out["delta"]["dice"] == out["after"]["dice"] - out["before"]["dice"]


def test_run_flags_unpaired_populations(tmp_path):
    """If the warp does not return one feature per input feature, before and after
    describe different cell sets and `delta` is not a pure registration effect."""
    native = _grid_fc(3, 3, 50, 20)                 # 9 cells
    dropped = _grid_fc(2, 2, 50, 20)                # warp came back with 4
    ref_gj = _write(tmp_path / "ref.geojson", native)
    mov_gj = _write(tmp_path / "mov.geojson", native)
    reg = _FakeRegistrar({"ref": _FakeSlide(dropped), "mov": _FakeSlide(dropped)})
    out = warp_seg_qc.run("pickle", "ref", "mov", ref_gj, mov_gj, loader=lambda _p: reg)
    assert out["counts"]["paired"] is False
    assert out["counts"]["features_ref_native"] == 9
    assert out["counts"]["features_ref_warped"] == 4

    ok = _FakeRegistrar({"ref": _FakeSlide(native), "mov": _FakeSlide(native)})
    assert warp_seg_qc.run("pickle", "ref", "mov", ref_gj, mov_gj,
                           loader=lambda _p: ok)["counts"]["paired"] is True


# ── rasterizer correctness ─────────────────────────────────────────────────────
def test_occluded_cell_is_not_counted_as_a_missed_match():
    """A cell fully overwritten by a later overlapping cell is absent from the raster.
    Counting it via max(label) inflated the recall denominator and depressed F1."""
    covered = _fc(_square(0, 0, 4), _square(0, 0, 4))     # feature 2 hides feature 1
    single = _fc(_square(0, 0, 4))
    out = warp_seg_qc._score(covered, single)
    assert out["n_features_ref"] == 2      # two features went in
    assert out["n_ref"] == 1               # one label survives rasterization
    assert out["instance_f1"] == 1.0       # so the surviving cell is a clean match


def test_holes_are_punched_out_of_their_own_feature_only():
    outer = [[0, 0], [20, 0], [20, 20], [0, 20], [0, 0]]
    hole = [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]]
    donut = {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [outer, hole]}}
    neighbour = _square(6, 6, 8)          # sits inside the donut's hole
    lab = warp_seg_qc.rasterize_features(_fc(donut, neighbour), (24, 24))
    assert lab[1, 1] == 1                 # donut wall kept
    assert lab[10, 10] == 2               # neighbour survives the hole punch
    lab_alone = warp_seg_qc.rasterize_features(_fc(donut), (24, 24))
    assert lab_alone[10, 10] == 0         # hole really is empty without a neighbour


def test_degenerate_rings_do_not_inflate_cell_counts():
    degenerate = {"type": "Feature", "properties": {},
                  "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}}
    fc = _fc(_square(10, 10, 6), degenerate)
    out = warp_seg_qc._score(fc, _fc(_square(10, 10, 6)))
    assert out["n_features_ref"] == 2 and out["n_ref"] == 1
    assert out["instance_f1"] == 1.0
