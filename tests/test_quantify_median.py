"""Equivalence tests for the memory-bounded median in compute_compartment_intensities.

The median computation was rewritten from a per-pixel pandas DataFrame + groupby
(one row per foreground pixel — O(image size), a WSI-scale memory blowup) to
scipy.ndimage.labeled_comprehension operating directly on the existing label/value
arrays (bounded memory). This test asserts the new path is numerically IDENTICAL
to the old one:

  1. against an independent per-label np.median reference, and
  2. against a verbatim copy of the OLD per-pixel-DataFrame groupby-median logic,
     on randomized synthetic images (seeded with np.random.default_rng(0)).

Empty-label and even-count (median-of-two-middles) semantics are covered
explicitly, since those are exactly the cases where a naive scipy/pandas swap
could silently diverge.
"""

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

_SPEC = importlib.util.spec_from_file_location(
    "quantify", Path(__file__).resolve().parent.parent / "bin" / "quantify.py"
)
quantify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(quantify)

compute_compartment_intensities = quantify.compute_compartment_intensities


def _old_compute_compartment_intensities(cell_mask, nuclei_mask, channel, channel_name):
    """Verbatim copy of the OLD per-pixel-DataFrame median algorithm (golden reference).

    This mirrors exactly what compute_compartment_intensities did before the
    labeled_comprehension rewrite: build one row per foreground pixel in a pandas
    DataFrame, then groupby(label).median(), with Nucleus/Cytoplasm compartments
    reindexed onto valid_labels and NaN (missing/empty group) filled with 0.0.
    """
    cell_mask = np.ascontiguousarray(cell_mask.squeeze())
    channel = channel.squeeze()
    has_nuclei = nuclei_mask is not None
    if has_nuclei:
        nuclei_mask = np.ascontiguousarray(nuclei_mask.squeeze())

    valid_labels = np.unique(cell_mask)
    valid_labels = valid_labels[valid_labels != 0]
    if len(valid_labels) == 0:
        return {}

    flat_cell = cell_mask.ravel()
    flat_val = channel.ravel().astype(np.float64)

    nuc_fg = None
    if has_nuclei:
        nuc_fg = nuclei_mask.ravel() > 0

    fg = flat_cell != 0
    px = {"label": flat_cell[fg], "val": flat_val[fg]}
    if has_nuclei:
        px["nuc"] = nuc_fg[fg]
    df_px = pd.DataFrame(px)
    medians = {"Cell": df_px.groupby("label")["val"].median()}
    if has_nuclei:
        medians["Nucleus"] = df_px[df_px["nuc"]].groupby("label")["val"].median()
        medians["Cytoplasm"] = df_px[~df_px["nuc"]].groupby("label")["val"].median()

    out = {}
    compartments = ["Cell"] + (["Nucleus", "Cytoplasm"] if has_nuclei else [])
    for comp in compartments:
        out[f"{channel_name}: {comp}: Median"] = (
            medians[comp].reindex(valid_labels).fillna(0.0).values
        )
    out["label"] = valid_labels
    return out


def _reference_median_per_label(cell_mask, values, labels):
    """Independent np.median-per-label reference (no pandas, no scipy)."""
    out = np.zeros(len(labels), dtype=np.float64)
    flat_cell = cell_mask.ravel()
    flat_val = values.ravel().astype(np.float64)
    for i, lab in enumerate(labels):
        sel = flat_val[flat_cell == lab]
        out[i] = np.median(sel) if sel.size else 0.0
    return out


def test_multiple_labels_varying_size():
    cell_mask = np.array(
        [
            [1, 1, 1, 2],
            [1, 1, 2, 2],
            [3, 0, 0, 2],
            [3, 3, 0, 2],
        ]
    )
    channel = np.array(
        [
            [10.0, 20.0, 30.0, 1.0],
            [40.0, 50.0, 2.0, 3.0],
            [60.0, 0.0, 0.0, 4.0],
            [70.0, 80.0, 0.0, 5.0],
        ]
    )
    df = compute_compartment_intensities(cell_mask, None, channel, "CD8")
    labels = df["label"].values
    ref = _reference_median_per_label(cell_mask, channel, labels)
    np.testing.assert_allclose(df["CD8: Cell: Median"].values, ref, rtol=0, atol=0)
    # Sanity on the expected values themselves.
    expected = {1: np.median([10, 20, 30, 40, 50]), 2: np.median([1, 2, 3, 4, 5]), 3: np.median([60, 70, 80])}
    for lab, val in zip(labels, df["CD8: Cell: Median"].values):
        assert val == expected[lab]


def test_single_pixel_label():
    cell_mask = np.array([[0, 0, 0], [0, 1, 0], [0, 0, 0]])
    channel = np.array([[0.0, 0.0, 0.0], [0.0, 42.5, 0.0], [0.0, 0.0, 0.0]])
    df = compute_compartment_intensities(cell_mask, None, channel, "CD8")
    assert df["CD8: Cell: Median"].values[0] == 42.5


def test_even_count_label_averages_two_middles():
    # Label 1 has exactly 4 foreground pixels -> median = mean of the two middle
    # sorted values. This exercises the tie/interpolation convention.
    cell_mask = np.array([[1, 1], [1, 1]])
    channel = np.array([[1.0, 2.0], [3.0, 4.0]])
    df = compute_compartment_intensities(cell_mask, None, channel, "CD8")
    # sorted [1,2,3,4] -> (2+3)/2 = 2.5
    assert df["CD8: Cell: Median"].values[0] == 2.5
    assert df["CD8: Cell: Median"].values[0] == np.median([1.0, 2.0, 3.0, 4.0])


def test_empty_compartment_defaults_to_zero():
    # Cell 1 has NO nuclear-mask overlap at all -> Nucleus median must default to
    # 0.0 (matching the old reindex(...).fillna(0.0) on a missing groupby key),
    # and Cytoplasm must equal the Cell median exactly (all cell pixels are
    # cytoplasm since none overlap the nucleus).
    cell_mask = np.array([[1, 1, 1], [1, 1, 1]])
    nuclei_mask = np.zeros_like(cell_mask)  # no nucleus anywhere
    channel = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    df = compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD8")
    assert df["CD8: Nucleus: Median"].values[0] == 0.0
    assert df["CD8: Cytoplasm: Median"].values[0] == df["CD8: Cell: Median"].values[0]

    # And the reverse: cell 2 is ENTIRELY nucleus -> Cytoplasm defaults to 0.0.
    cell_mask2 = np.array([[2, 2], [2, 2]])
    nuclei_mask2 = np.array([[1, 1], [1, 1]])
    channel2 = np.array([[10.0, 20.0], [30.0, 40.0]])
    df2 = compute_compartment_intensities(cell_mask2, nuclei_mask2, channel2, "CD8")
    assert df2["CD8: Cytoplasm: Median"].values[0] == 0.0
    assert df2["CD8: Nucleus: Median"].values[0] == df2["CD8: Cell: Median"].values[0]


def test_background_label_zero_excluded():
    cell_mask = np.array([[0, 0, 1], [0, 1, 1], [0, 0, 0]])
    channel = np.array([[999.0, 999.0, 5.0], [999.0, 7.0, 9.0], [999.0, 999.0, 999.0]])
    df = compute_compartment_intensities(cell_mask, None, channel, "CD8")
    assert list(df["label"].values) == [1]
    assert df["CD8: Cell: Median"].values[0] == np.median([5.0, 7.0, 9.0])


def test_golden_old_vs_new_random_images_no_nuclei():
    rng = np.random.default_rng(0)
    for _trial in range(25):
        h, w = rng.integers(5, 30), rng.integers(5, 30)
        n_labels = rng.integers(1, 8)
        cell_mask = rng.integers(0, n_labels + 1, size=(h, w))
        channel = rng.random((h, w)) * 1000.0

        new_df = compute_compartment_intensities(cell_mask, None, channel, "CD8")
        old = _old_compute_compartment_intensities(cell_mask, None, channel, "CD8")

        if len(old) == 0:
            assert new_df.empty
            continue

        np.testing.assert_array_equal(new_df["label"].values, old["label"])
        np.testing.assert_allclose(
            new_df["CD8: Cell: Median"].values,
            old["CD8: Cell: Median"],
            rtol=0,
            atol=0,
        )


def test_golden_old_vs_new_random_images_with_nuclei():
    rng = np.random.default_rng(0)
    for _trial in range(25):
        h, w = rng.integers(5, 30), rng.integers(5, 30)
        n_labels = rng.integers(1, 8)
        cell_mask = rng.integers(0, n_labels + 1, size=(h, w))
        # Nuclei mask: independent random binary overlay (need not share labels with
        # cell_mask — compute_compartment_intensities only uses nuclei_mask > 0).
        nuclei_mask = (rng.random((h, w)) > 0.5).astype(np.int32)
        channel = rng.random((h, w)) * 1000.0

        new_df = compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD8")
        old = _old_compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD8")

        if len(old) == 0:
            assert new_df.empty
            continue

        np.testing.assert_array_equal(new_df["label"].values, old["label"])
        for comp in ("Cell", "Nucleus", "Cytoplasm"):
            key = f"CD8: {comp}: Median"
            np.testing.assert_allclose(
                new_df[key].values, old[key], rtol=0, atol=0
            )


def test_no_per_pixel_dataframe_in_source():
    """Sanity: the median branch must no longer construct a per-pixel DataFrame."""
    import inspect

    src = inspect.getsource(compute_compartment_intensities)
    # Isolate the median block (between the MEDIAN comment and the `if expanded:`
    # branch) and make sure it contains no pd.DataFrame / groupby call.
    start = src.index("Default statistic: MEDIAN")
    end = src.index("if expanded:")
    median_block = src[start:end]
    assert "pd.DataFrame" not in median_block
    assert ".groupby(" not in median_block
    assert "labeled_comprehension" in median_block
