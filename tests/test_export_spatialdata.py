"""Unit tests for bin/export_spatialdata.py.

Covers the pure-logic layer: measurement-key parsing, QC sanitizing, and the
spatial join of registration residuals onto segmentation labels. The full
SpatialData round-trip needs `spatialdata`/`geopandas`, which live only in the
export container, so it is exercised by the nf-test suite rather than here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(BIN / "utils"))

spec = importlib.util.spec_from_file_location(
    "export_spatialdata", BIN / "export_spatialdata.py"
)
esd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(esd)


# ── measurement keys ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "key,expected",
    [
        ("CD3: Nucleus: Median", ("CD3", "Nucleus", "Median")),
        ("CD8: Cytoplasm: Mean", ("CD8", "Cytoplasm", "Mean")),
        ("PanCK: Cell: Sum", ("PanCK", "Cell", "Sum")),
        # A bare marker is NOT assumed to be whole-cell mean — var must not assert
        # a compartment the pipeline never measured.
        ("DAPI", ("DAPI", None, None)),
        # Marker names may themselves contain ": "; only the last two tokens count.
        (
            "CD3: clone UCHT1: Nucleus: Median",
            ("CD3: clone UCHT1", "Nucleus", "Median"),
        ),
    ],
)
def test_parse_measurement_key(key, expected):
    assert esd.parse_measurement_key(key) == expected


def test_parse_measurement_key_rejects_empty_marker():
    """': Nucleus: Median' has no marker, so it is not a compartment key."""
    assert esd.parse_measurement_key(": Nucleus: Median") == (
        ": Nucleus: Median",
        None,
        None,
    )


def test_identify_marker_columns_excludes_morphology():
    df = pd.DataFrame(
        {
            "label": [1, 2],
            "x": [1.0, 2.0],
            "y": [3.0, 4.0],
            "area": [10, 20],
            "CD3: Cell: Median": [1.5, 2.5],
            "DAPI": [7.0, 8.0],
            "some_text": ["a", "b"],
        }
    )
    assert esd.identify_marker_columns(df) == ["CD3: Cell: Median", "DAPI"]


# ── QC sanitizing ──────────────────────────────────────────────────────────────
def test_sanitize_converts_none_to_nan():
    """warp_seg_qc emits `radius_factor: None`; None does not survive AnnData->Zarr."""
    out = esd.sanitize_for_uns({"radius_factor": None, "n_pairs": 12})
    assert np.isnan(out["radius_factor"])
    assert out["n_pairs"] == 12


def test_sanitize_coerces_numpy_scalars():
    out = esd.sanitize_for_uns(
        {"a": np.int64(3), "b": np.float32(1.5), "c": np.bool_(True)}
    )
    assert (
        isinstance(out["a"], int) and isinstance(out["b"], float) and out["c"] is True
    )


def test_sanitize_recurses_and_stringifies_keys():
    out = esd.sanitize_for_uns({1: {"deep": [np.int64(1), None]}})
    assert "1" in out
    assert out["1"]["deep"][0] == 1


def test_load_qc_keeps_verbatim_json_and_survives_bad_files(tmp_path):
    good = tmp_path / "a_seg_qc.json"
    good.write_text(
        json.dumps({"moving": "cycle2", "matching": {"radius_factor": None}})
    )
    bad = tmp_path / "broken.json"
    bad.write_text("{not json")

    seg = tmp_path / "s_seg_eval.json"
    seg.write_text(
        json.dumps({"id": "P1", "QualityScore": 0.87, "downsample_factor": 2})
    )

    qc, extras = esd.load_qc([str(good), str(bad)], [str(seg)], [])
    assert "cycle2" in qc["registration"]
    assert np.isnan(qc["registration"]["cycle2"]["matching"]["radius_factor"])
    # verbatim copy is preserved alongside the flattened form
    assert (
        json.loads(extras["qc_json"]["registration"]["cycle2"])["matching"][
            "radius_factor"
        ]
        is None
    )
    assert qc["segmentation"]["P1"]["QualityScore"] == 0.87


# ── spatial join of registration residuals ─────────────────────────────────────
def _residual_csv(path, rows, moving="cycle2"):
    pd.DataFrame(
        [
            {
                "moving": moving,
                "ref_x": x,
                "ref_y": y,
                "residual_px": r,
                "stage": "micro",
            }
            for x, y, r in rows
        ]
    ).to_csv(path, index=False)
    return str(path)


def test_join_reg_residuals_matches_nearest_centroid(tmp_path):
    centroids = np.array([[0.0, 0.0], [100.0, 100.0]])
    labels = np.array([7, 9])
    csv = _residual_csv(tmp_path / "r.csv", [(0.5, 0.5, 2.0), (100.2, 99.8, 5.0)])

    out, stats = esd.join_reg_residuals([csv], centroids, labels, max_dist_px=15.0)
    assert list(out.index) == [7, 9]
    assert out["cycle2"].tolist() == [2.0, 5.0]
    assert stats["slides"]["cycle2"]["joined"] == 2


def test_join_reg_residuals_drops_pairs_beyond_radius(tmp_path):
    """An unmatched cell means 'no QC evidence', never 'well registered'."""
    centroids = np.array([[0.0, 0.0]])
    labels = np.array([1])
    csv = _residual_csv(tmp_path / "r.csv", [(500.0, 500.0, 3.0)])

    out, stats = esd.join_reg_residuals([csv], centroids, labels, max_dist_px=15.0)
    assert np.isnan(out["cycle2"].iloc[0])
    assert stats["slides"]["cycle2"]["joined"] == 0
    assert stats["slides"]["cycle2"]["cell_coverage"] == 0.0


def test_join_reg_residuals_keeps_worst_on_collision(tmp_path):
    """Two QC cells on one mask cell -> keep the WORST residual.

    This column exists to exclude badly-registered cells; taking the optimistic
    value would hide exactly the cells it is meant to flag.
    """
    centroids = np.array([[0.0, 0.0]])
    labels = np.array([1])
    csv = _residual_csv(tmp_path / "r.csv", [(0.1, 0.1, 1.0), (0.2, 0.2, 9.0)])

    out, _ = esd.join_reg_residuals([csv], centroids, labels, max_dist_px=15.0)
    assert out["cycle2"].iloc[0] == 9.0


def test_join_reg_residuals_one_column_per_moving_slide(tmp_path):
    centroids = np.array([[0.0, 0.0]])
    labels = np.array([1])
    c2 = _residual_csv(tmp_path / "c2.csv", [(0.0, 0.0, 2.0)], moving="cycle2")
    c3 = _residual_csv(tmp_path / "c3.csv", [(0.0, 0.0, 4.0)], moving="cycle3")

    out, stats = esd.join_reg_residuals([c2, c3], centroids, labels, max_dist_px=15.0)
    assert sorted(out.columns) == ["cycle2", "cycle3"]
    assert out.loc[1, "cycle2"] == 2.0 and out.loc[1, "cycle3"] == 4.0
    assert set(stats["slides"]) == {"cycle2", "cycle3"}


def test_join_reg_residuals_handles_no_inputs():
    out, stats = esd.join_reg_residuals([], np.empty((0, 2)), np.array([1, 2]), 15.0)
    assert out.empty and stats["slides"] == {}


def test_join_reg_residuals_skips_header_only_csv(tmp_path):
    """The stub (and a slide that paired nothing) writes a header-only CSV."""
    p = tmp_path / "empty.csv"
    p.write_text("moving,ref_x,ref_y,residual_px,stage\n")
    out, stats = esd.join_reg_residuals(
        [str(p)], np.array([[0.0, 0.0]]), np.array([1]), 15.0
    )
    assert out.empty and stats["slides"] == {}
