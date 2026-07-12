"""Tests for bin/mask_to_geojson.py — label mask -> cell GeoJSON (the glue between
segment.py's cell_mask.tif and warp_seg_qc's warp input)."""
from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("skimage")
pytest.importorskip("scipy")

import mask_to_geojson
import warp_seg_qc


def _two_square_mask():
    m = np.zeros((20, 20), dtype=np.uint32)
    m[2:8, 2:8] = 1
    m[12:18, 12:18] = 2
    return m


def test_each_label_becomes_one_closed_polygon():
    fc = mask_to_geojson.mask_to_feature_collection(_two_square_mask())
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    assert sorted(f["properties"]["label"] for f in fc["features"]) == [1, 2]
    for ft in fc["features"]:
        ring = ft["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1]


def test_ignores_background_only_mask():
    fc = mask_to_geojson.mask_to_feature_collection(np.zeros((8, 8), dtype=np.uint32))
    assert fc["features"] == []


def test_roundtrip_mask_to_geojson_to_raster_recovers_foreground():
    m = _two_square_mask()
    fc = mask_to_geojson.mask_to_feature_collection(m)
    shape_rc, offset = warp_seg_qc.common_grid(fc, pad=0)
    lab = warp_seg_qc.rasterize_features(fc, shape_rc, offset)
    orig = m > 0
    recov = np.zeros_like(orig)
    ys, xs = np.nonzero(lab > 0)
    recov[ys + int(offset[1]), xs + int(offset[0])] = True
    inter = np.logical_and(orig, recov).sum()
    union = np.logical_or(orig, recov).sum()
    assert inter / union > 0.7


def test_write_geojson_roundtrips_through_disk(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    mask_path = tmp_path / "P001_cell_mask.tif"
    tifffile.imwrite(str(mask_path), _two_square_mask())
    out = tmp_path / "P001_cells.geojson"
    n = mask_to_geojson.write_geojson(str(mask_path), str(out))
    assert n == 2
    assert len(json.loads(out.read_text())["features"]) == 2
