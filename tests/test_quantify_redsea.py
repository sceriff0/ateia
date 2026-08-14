"""REDSEA's integration into bin/quantify.py.

tests/test_redsea.py pins the algorithm. This file pins the seam: which columns
appear, which markers are touched, and — most importantly — that a marker NOT in
--redsea_markers comes out byte-identical to a run with REDSEA off. REDSEA is
sold as purely additive, and that claim is only true if this holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN / "utils"))
sys.path.insert(0, str(BIN))

import quantify  # noqa: E402
import redsea  # noqa: E402


@pytest.fixture
def scene():
    """Two touching cell blocks: cell 1 is the only true source of the marker.

    A fixed fraction of cell 1's membrane signal is painted into the strip of
    cell 2 nearest the shared interface — a synthetic but faithful lateral leak.
    """
    mask = np.zeros((40, 40), dtype=np.int32)
    mask[:, :20] = 1
    mask[:, 20:] = 2
    geom = redsea.redsea_geometry(mask, element_size=4)
    channel = np.zeros(mask.shape, dtype=np.float32)
    channel[(mask == 1) & geom.band] = 100.0
    channel[(mask == 2) & geom.band] = 40.0  # the leak
    return mask, geom, channel


def _redsea_tuple(geom, checker=1):
    return (geom.weights, geom.band, checker)


# ── column contract ───────────────────────────────────────────────────────────
def test_redsea_adds_exactly_one_whole_cell_column(scene):
    """ONE metric, not two. It is the size-normalised compensated value -- the
    reference implementation's own recommended output (dataRedSeaScaleSizeFCS)."""
    mask, geom, channel = scene
    plain = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=["Median"]
    )
    comped = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=["Median", "REDSEA"],
        redsea=_redsea_tuple(geom),
    )
    added = [c for c in comped.columns if c not in plain.columns]
    assert added == ["CD3: Cell: REDSEA"]


def test_redsea_leaves_every_pre_existing_column_untouched(scene):
    """The additive claim, tested. Not "roughly the same" — identical."""
    mask, geom, channel = scene
    base = ["Median", "Mean", "Sum"]
    plain = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=base
    )
    comped = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=base + ["REDSEA"],
        redsea=_redsea_tuple(geom),
    )
    pd.testing.assert_frame_equal(comped[plain.columns], plain)


def test_redsea_is_whole_cell_only_even_with_a_nuclear_mask(scene):
    """No REDSEA Nucleus/Cytoplasm columns: the method has no compartment split."""
    mask, geom, channel = scene
    nuclei = np.zeros_like(mask)
    nuclei[15:25, 5:15] = 1
    nuclei[15:25, 25:35] = 2
    df = quantify.compute_compartment_intensities(
        mask, nuclei, channel, "CD3",
        statistics=["Median", "Mean", "Sum", "REDSEA"], redsea=_redsea_tuple(geom),
    )
    redsea_cols = [c for c in df.columns if "REDSEA" in c]
    assert redsea_cols == ["CD3: Cell: REDSEA"]


def test_redsea_is_the_size_normalised_compensated_value(scene):
    """The single metric == compensated integrated counts / cell area.

    Pinned against redsea.compensate directly, so the column cannot silently
    become the raw compensated sum (which is a different, much larger number).
    """
    mask, geom, channel = scene
    df = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=["REDSEA"], redsea=_redsea_tuple(geom)
    )
    whole, edge = redsea.band_sums(mask, geom.band, channel,
                                   n_labels=geom.weights.shape[0])
    comp = redsea.compensate(whole, edge, geom.weights, redsea_checker=1)
    labels = df["label"].to_numpy()
    areas = np.bincount(mask.ravel(), minlength=geom.weights.shape[0])[labels]
    assert df["CD3: Cell: REDSEA"].to_numpy() == pytest.approx(comp[labels] / areas)


# ── the correction actually corrects ──────────────────────────────────────────
def test_spillover_only_cell_is_suppressed(scene):
    mask, geom, channel = scene
    df = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=["Median","Mean","Sum","REDSEA"],
        redsea=_redsea_tuple(geom)
    )
    leaked_area = int(np.bincount(mask.ravel())[2])
    leaked = df.loc[df.label == 2].iloc[0]
    assert leaked["CD3: Cell: REDSEA"] * leaked_area < leaked["CD3: Cell: Sum"] * 0.5


def test_true_positive_cell_keeps_its_signal(scene):
    mask, geom, channel = scene
    df = quantify.compute_compartment_intensities(
        mask, None, channel, "CD3", statistics=["Median","Mean","Sum","REDSEA"],
        redsea=_redsea_tuple(geom)
    )
    src_area = int(np.bincount(mask.ravel())[1])
    src = df.loc[df.label == 1].iloc[0]
    assert src["CD3: Cell: REDSEA"] * src_area >= src["CD3: Cell: Sum"] * 0.8


# ── shape guards ──────────────────────────────────────────────────────────────
def test_geometry_from_a_different_mask_is_rejected(scene):
    """Silent misalignment is the dangerous failure: same cell count, wrong pixels."""
    mask, geom, channel = scene
    other = np.zeros((30, 30), dtype=np.int32)
    other[:, :15] = 1
    other[:, 15:] = 2
    wrong = redsea.redsea_geometry(other, element_size=4)
    with pytest.raises(ValueError, match="band/mask shape mismatch"):
        quantify.compute_compartment_intensities(
            mask, None, channel, "CD3", statistics=["REDSEA"], redsea=_redsea_tuple(wrong)
        )


def test_geometry_with_too_few_labels_is_rejected():
    mask = np.zeros((20, 40), dtype=np.int32)
    mask[:, :20] = 1
    mask[:, 20:] = 2
    channel = np.ones(mask.shape, dtype=np.float32)
    small = np.zeros((20, 20), dtype=np.int32)
    small[:] = 1
    geom = redsea.redsea_geometry(small, element_size=2)
    band = np.zeros(mask.shape, dtype=bool)
    with pytest.raises(ValueError, match="labels"):
        quantify.compute_compartment_intensities(
            mask, None, channel, "CD3", statistics=["REDSEA"], redsea=(geom.weights, band, 1)
        )


# ── the marker opt-in rule ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,markers,expected",
    [
        ("CD3", ["CD3", "CD8"], True),
        ("cd3", ["CD3"], True),
        (" CD3 ", ["CD3"], True),
        ("CD3", "CD3,CD8", True),
        ("DAPI", ["CD3", "CD8"], False),
        ("CD3", [], False),
        ("CD3", None, False),
        # The one that matters: EXACT, not substring. A substring rule would make
        # --redsea_markers CD4 silently compensate CD45 too, and compensating the
        # wrong marker produces plausible numbers rather than an error.
        ("CD45", ["CD4"], False),
        ("CD4", ["CD45"], False),
    ],
)
def test_is_redsea_marker(name, markers, expected):
    assert quantify.is_redsea_marker(name, markers) is expected


# ── the run_quantification seam ───────────────────────────────────────────────
def _write_scene(tmp_path, mask, channel, geom):
    import tifffile
    from redsea_matrix import save_geometry

    np.save(tmp_path / "mask.npy", mask)
    tifffile.imwrite(tmp_path / "CD3.tif", channel)
    save_geometry(geom, tmp_path / "g.npz")
    return tmp_path


def test_opted_out_marker_matches_a_redsea_off_run_exactly(tmp_path, scene):
    """The additive claim again, this time through the real CLI-level entry point.

    A REDSEA-enabled run hands EVERY marker task a geometry; only the opted-in
    ones may differ. If this ever fails, turning REDSEA on has changed numbers for
    markers the user never asked to compensate.
    """
    mask, geom, channel = scene
    d = _write_scene(tmp_path, mask, channel, geom)

    off = quantify.run_quantification(
        str(d / "mask.npy"), str(d / "CD3.tif"), str(d / "off.csv"),
        channel_name="DAPI", statistics=["Median","Mean","Sum"],
    )
    on = quantify.run_quantification(
        str(d / "mask.npy"), str(d / "CD3.tif"), str(d / "on.csv"),
        channel_name="DAPI", statistics=["Median","Mean","Sum","REDSEA"],
        redsea_geometry_path=str(d / "g.npz"), redsea_markers=["CD3"],
    )
    pd.testing.assert_frame_equal(on, off)
    assert not any("REDSEA" in c for c in on.columns)


def test_opted_in_marker_gains_the_columns_through_the_cli_seam(tmp_path, scene):
    mask, geom, channel = scene
    d = _write_scene(tmp_path, mask, channel, geom)
    df = quantify.run_quantification(
        str(d / "mask.npy"), str(d / "CD3.tif"), str(d / "on.csv"),
        channel_name="CD3", statistics=["Median","REDSEA"],
        redsea_geometry_path=str(d / "g.npz"), redsea_markers="CD3,CD8",
    )
    assert "CD3: Cell: REDSEA" in df.columns
    written = pd.read_csv(d / "on.csv")
    assert "CD3: Cell: REDSEA" in written.columns


def test_checker_zero_reaches_the_algebra(tmp_path, scene):
    """--redsea-checker must be threaded, not dropped on the floor."""
    mask, geom, channel = scene
    d = _write_scene(tmp_path, mask, channel, geom)
    kw = dict(
        channel_name="CD3", statistics=["Median", "REDSEA"],
        redsea_geometry_path=str(d / "g.npz"), redsea_markers=["CD3"],
    )
    one = quantify.run_quantification(
        str(d / "mask.npy"), str(d / "CD3.tif"), str(d / "a.csv"), redsea_checker=1, **kw
    )
    zero = quantify.run_quantification(
        str(d / "mask.npy"), str(d / "CD3.tif"), str(d / "b.csv"), redsea_checker=0, **kw
    )
    assert not np.allclose(
        one["CD3: Cell: REDSEA"], zero["CD3: Cell: REDSEA"]
    ), "redsea_checker had no effect — it is not reaching redsea.compensate"
