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

    qc, extras = esd.load_qc([str(good), str(bad)], [])
    assert "cycle2" in qc["registration"]
    assert np.isnan(qc["registration"]["cycle2"]["matching"]["radius_factor"])
    # verbatim copy is preserved alongside the flattened form
    assert (
        json.loads(extras["qc_json"]["registration"]["cycle2"])["matching"][
            "radius_factor"
        ]
        is None
    )


# ── spatial join of registration residuals ─────────────────────────────────────
def _residual_csv(path, rows, moving="cycle2", patient_id=None):
    """Write a residual CSV.

    ``patient_id=None`` writes the pre-Task-1 header (no ``patient_id`` column),
    which is what a residual CSV from an older run looks like — the join has to
    stay usable on those, so most tests below keep using that shape.
    """
    pd.DataFrame(
        [
            {
                **({"patient_id": patient_id} if patient_id is not None else {}),
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


def _qc_json(path, patient_id, moving):
    path.write_text(
        json.dumps({"patient_id": patient_id, "moving": moving, "stages_separable": True})
    )
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


# ── patient identity: one export, one patient ──────────────────────────────────
def test_two_patients_qc_does_not_leak_into_each_others_export(tmp_path):
    """A patient's export carries that patient's QC and nothing else.

    The two patients here have cells at the SAME reference-frame pixel
    coordinates. That is not a coincidence to be engineered away: each patient's
    reference frame is its own pixel grid rooted at (0, 0), so overlapping
    coordinates across patients are the normal case and mean nothing.

    Before this was fixed, `subworkflows/local/postprocess.nf` collected the
    reg_qc=2 artifacts run-wide and handed the whole list to a PER-PATIENT
    EXPORT_SPATIALDATA. P002's residual rows then landed within
    `spatialdata_residual_join_max_px` of P001's cells and were written into
    P001's `.zarr` as an obsm column named after a slide P001 never had, with a
    plausible-looking `join_fraction` to match; P002's QC doc likewise appeared
    under P001's `uns["qc"]["registration"]`.
    """
    centroids = np.array([[10.0, 10.0], [40.0, 40.0]])
    labels = np.array([1, 2])

    p001 = _residual_csv(
        tmp_path / "P001_mov1_reg_residuals.csv",
        [(10.0, 10.0, 0.5), (40.0, 40.0, 0.7)],
        moving="P001_mov1",
        patient_id="P001",
    )
    p002 = _residual_csv(
        tmp_path / "P002_mov1_reg_residuals.csv",
        [(10.0, 10.0, 9.0), (40.0, 40.0, 8.0)],
        moving="P002_mov1",
        patient_id="P002",
    )
    qc001 = _qc_json(tmp_path / "P001_mov1_seg_qc.json", "P001", "P001_mov1")
    qc002 = _qc_json(tmp_path / "P002_mov1_seg_qc.json", "P002", "P002_mov1")

    # Handed both patients' artifacts — exactly what the run-wide collect() did —
    # the export REFUSES. A foreign patient's coordinates are not a filterable
    # nuisance: they are meaningless in this patient's frame, so joining them is
    # never the right answer and neither is quietly dropping them.
    with pytest.raises(ValueError, match="P002"):
        esd.join_reg_residuals([p001, p002], centroids, labels, 15.0)
    with pytest.raises(ValueError, match="P002"):
        esd.load_qc([qc001, qc002], [])

    # Wired per patient, P001's export carries only P001's residual column and
    # only P001's registration QC entry.
    out, stats = esd.join_reg_residuals(
        [p001], centroids, labels, 15.0, patient_id="P001"
    )
    assert list(out.columns) == ["P001_mov1"]
    assert set(stats["slides"]) == {"P001_mov1"}
    assert out["P001_mov1"].tolist() == [0.5, 0.7]

    qc, extras = esd.load_qc([qc001], [], patient_id="P001")
    assert set(qc["registration"]) == {"P001_mov1"}
    assert set(extras["qc_json"]["registration"]) == {"P001_mov1"}


def test_join_fraction_counts_only_this_patients_slides(tmp_path):
    """`join_fraction` is a per-slide statistic over THIS patient's rows.

    Two moving slides for P001, one of which pairs a cell the mask does not have.
    The fractions must be computed slide by slide over that slide's own rows —
    not diluted or inflated by another patient's rows sharing the coordinate.
    """
    centroids = np.array([[10.0, 10.0]])
    labels = np.array([1])

    a = _residual_csv(
        tmp_path / "a.csv",
        [(10.0, 10.0, 1.0), (900.0, 900.0, 2.0)],
        moving="P001_mov1",
        patient_id="P001",
    )
    b = _residual_csv(
        tmp_path / "b.csv", [(10.0, 10.0, 3.0)], moving="P001_mov2", patient_id="P001"
    )

    _, stats = esd.join_reg_residuals([a, b], centroids, labels, 15.0, patient_id="P001")
    assert set(stats["slides"]) == {"P001_mov1", "P001_mov2"}
    assert stats["slides"]["P001_mov1"]["join_fraction"] == 0.5
    assert stats["slides"]["P001_mov2"]["join_fraction"] == 1.0


def test_foreign_patient_residual_row_raises(tmp_path):
    """A residual row naming another patient is an error, not something to filter."""
    foreign = _residual_csv(
        tmp_path / "P002.csv", [(0.0, 0.0, 1.0)], moving="P002_mov1", patient_id="P002"
    )
    with pytest.raises(ValueError) as exc:
        esd.join_reg_residuals(
            [foreign], np.array([[0.0, 0.0]]), np.array([1]), 15.0, patient_id="P001"
        )
    assert "P002" in str(exc.value) and "P001" in str(exc.value)


def test_foreign_patient_qc_doc_raises(tmp_path):
    """A registration QC doc naming another patient is an error too."""
    foreign = _qc_json(tmp_path / "P002_seg_qc.json", "P002", "P002_mov1")
    with pytest.raises(ValueError) as exc:
        esd.load_qc([foreign], [], patient_id="P001")
    assert "P002" in str(exc.value) and "P001" in str(exc.value)


def test_residual_csv_without_patient_id_column_is_still_joined(tmp_path):
    """A CSV from before the `patient_id` column existed stays usable.

    The channel-level join is what guarantees only this patient's files arrive;
    the in-CSV check is defence in depth behind it, and defence in depth must not
    refuse to run on data that predates it.
    """
    legacy = _residual_csv(tmp_path / "legacy.csv", [(0.0, 0.0, 2.0)], moving="cycle2")
    out, stats = esd.join_reg_residuals(
        [legacy], np.array([[0.0, 0.0]]), np.array([1]), 15.0, patient_id="P001"
    )
    assert out["cycle2"].iloc[0] == 2.0
    assert stats["slides"]["cycle2"]["joined"] == 1
