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
