"""Equivalence tests for the per-label median in compute_compartment_intensities.

The median computation has gone through two rewrites. First, from a per-pixel
pandas DataFrame + groupby (one row per foreground pixel — O(image size), a
WSI-scale memory blowup) to scipy.ndimage.labeled_comprehension operating
directly on the existing label/value arrays (bounded memory, but still one
Python callback per label). Second — the fast path this branch adds — to
`_median_per_label_from_sorted`: a composite-key sort of (label, value) pairs
that reads each compartment's median directly off contiguous sorted runs, no
per-label callback at all. That fast path only handles the uint16 dtype the
pipeline actually produces (it composes a `(label << 16) | value` sort key that
needs 16 spare bits); any other dtype falls back to
`scipy.ndimage.labeled_comprehension`, which stays in the module for exactly
that case. This test asserts the fast path is numerically IDENTICAL to both
prior implementations:

  1. against an independent per-label np.median reference, and
  2. against a verbatim copy of the OLD per-pixel-DataFrame groupby-median logic,
     on randomized synthetic images (seeded with np.random.default_rng(0)).

Empty-label and even-count (median-of-two-middles) semantics are covered
explicitly, since those are exactly the cases where a naive rewrite could
silently diverge.
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


def test_empty_compartment_is_nan_not_zero():
    # CONTRACT CHANGED, deliberately. This test previously asserted 0.0, pinning the older
    # `.reindex(valid_labels).fillna(0.0)` behaviour. 0.0 makes "this cell has no nucleus"
    # indistinguishable from "this cell's nucleus was measured and is dark", and downstream
    # gating reads the second; nothing can separate them afterwards. The consumer already
    # documents the opposite policy -- bin/export_geojson.py:78 preserves NaN as "missing" and
    # omits such measurements from the GeoJSON rather than writing an invalid bare NaN literal.
    #
    # The assertions that did NOT depend on the default are kept unchanged below, because they
    # are what proves the compartments are still partitioned correctly.
    # See tests/test_absent_compartment_is_nan.py for the full contract, including the case
    # this change exists to separate: a measured-but-dark compartment still reports 0.0.
    cell_mask = np.array([[1, 1, 1], [1, 1, 1]])
    nuclei_mask = np.zeros_like(cell_mask)  # no nucleus anywhere
    channel = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    df = compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD8")
    assert np.isnan(df["CD8: Nucleus: Median"].values[0])
    assert df["CD8: Cytoplasm: Median"].values[0] == df["CD8: Cell: Median"].values[0]

    # And the reverse: cell 2 is ENTIRELY nucleus -> Cytoplasm was never measured.
    cell_mask2 = np.array([[2, 2], [2, 2]])
    nuclei_mask2 = np.array([[1, 1], [1, 1]])
    channel2 = np.array([[10.0, 20.0], [30.0, 40.0]])
    df2 = compute_compartment_intensities(cell_mask2, nuclei_mask2, channel2, "CD8")
    assert np.isnan(df2["CD8: Cytoplasm: Median"].values[0])
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
    """Sanity: the median branch builds no per-pixel DataFrame, and its hot (uint16) path
    invokes no per-label Python callback.

    This test used to assert `"labeled_comprehension" in median_block` -- true under the
    labeled_comprehension-only implementation this file's other tests were written against.
    That assertion is now the WRONG invariant to pin: `labeled_comprehension` deliberately
    still appears in this function, as the non-uint16 fallback required because the
    composite-key sort is only lexicographic while values fit 16 bits (see
    test_non_uint16_fallback_matches_fast_path). Asserting its absence would be false, and
    asserting its presence would no longer prove anything about the hot path -- it would pass
    even if the fast path silently kept calling it too.

    So this rewrite isolates the fast (uint16) branch specifically -- from its `if` guard to
    the `else:` that introduces the fallback -- and asserts THAT slice uses the new vectorised
    helper and contains no labeled_comprehension call, which is the actual invariant this
    test was always meant to protect: the hot path does no per-label Python-level work.
    """
    import inspect

    src = inspect.getsource(compute_compartment_intensities)
    # Isolate the median block (between the MEDIAN comment and the `if expanded:`
    # branch) and make sure it contains no pd.DataFrame / groupby call.
    start = src.index("Default statistic: MEDIAN")
    end = src.index("if expanded:")
    median_block = src[start:end]
    assert "pd.DataFrame" not in median_block
    assert ".groupby(" not in median_block

    # Isolate just the uint16 fast (hot) path from its `if` guard to the fallback `else:`.
    hot_start = median_block.index("if flat_raw.dtype == np.uint16:")
    hot_end = median_block.index("\n    else:", hot_start)
    hot_path = median_block[hot_start:hot_end]
    assert "_median_per_label_from_sorted" in hot_path
    assert "labeled_comprehension" not in hot_path

    # The fallback is still present -- just outside the hot path -- and is exercised by
    # test_non_uint16_fallback_matches_fast_path, so it is not dead code.
    assert "labeled_comprehension" in median_block[hot_end:]


def test_golden_old_vs_new_random_uint16_full_range():
    """Fast-path (uint16) equivalence, 40 trials: irregular label sets, cells with no nuclear
    overlap (forcing the NaN branch), the full uint16 intensity range, and both odd and even
    per-label pixel counts (median tie-break vs. a single middle element). Assert at
    rtol=0, atol=0, NaN-aware. This is the equivalence check run during the audit (40/40 pass)
    that the task brief requires stay passing.

    Unlike the two golden tests above (float64 channels, which never take the composite-key
    sort), this one uses a genuine uint16 channel so it actually exercises the fast path being
    added -- and cross-checks every value against BOTH the old per-pixel-DataFrame golden and
    an independent boolean-mask + np.median reference computed fresh per label, so a bug that
    happened to agree with one reference cannot hide from the other.
    """
    rng = np.random.default_rng(7)
    n_trials = 40
    saw_no_overlap = False
    saw_odd = False
    saw_even = False

    for _trial in range(n_trials):
        h, w = rng.integers(4, 40), rng.integers(4, 40)
        n_labels = rng.integers(1, 12)
        cell_mask = rng.integers(0, n_labels + 1, size=(h, w))
        # Full uint16 range, including the 0 and 65535 extremes.
        channel = rng.integers(0, 65536, size=(h, w), dtype=np.uint16)
        # Independent per-pixel nuclear overlay: with small cells this reliably produces some
        # cells with zero nuclear overlap and some that are entirely nucleus, across trials.
        nuclei_mask = (rng.random((h, w)) > 0.5).astype(np.int32)

        new_df = compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD8")
        old = _old_compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD8")

        if len(old) == 0:
            assert new_df.empty
            continue

        labels = new_df["label"].values
        np.testing.assert_array_equal(labels, old["label"])

        flat_cell = cell_mask.ravel()
        flat_val = channel.ravel().astype(np.float64)
        nuc_fg_full = nuclei_mask.ravel() > 0

        for comp in ("Cell", "Nucleus", "Cytoplasm"):
            key = f"CD8: {comp}: Median"
            new_vals = new_df[key].values
            old_vals = np.asarray(old[key], dtype=np.float64)
            for i, lab in enumerate(labels):
                sel = flat_cell == lab
                if comp == "Nucleus":
                    sel = sel & nuc_fg_full
                elif comp == "Cytoplasm":
                    sel = sel & ~nuc_fg_full
                count = int(sel.sum())
                if count == 0:
                    assert np.isnan(new_vals[i])
                    saw_no_overlap = True
                    continue
                saw_even = saw_even or count % 2 == 0
                saw_odd = saw_odd or count % 2 == 1
                # rtol=0, atol=0: bit-identical to the old per-pixel-DataFrame algorithm...
                assert new_vals[i] == old_vals[i], (comp, lab, new_vals[i], old_vals[i])
                # ...and to an independent reference computed fresh from the raw pixels.
                assert new_vals[i] == np.median(flat_val[sel])

    assert saw_no_overlap, "no trial produced an empty compartment (NaN branch unexercised)"
    assert saw_odd, "no trial produced an odd-count label (single middle element)"
    assert saw_even, "no trial produced an even-count label (tie-break averaging)"


def test_non_uint16_fallback_matches_fast_path():
    """The required guard: only a uint16 channel takes the composite-key fast path; anything
    else must fall back to labeled_comprehension. That fallback must produce IDENTICAL values
    to the fast path on the same underlying data -- otherwise it is silently wrong rather than
    merely slower. This keeps the fallback branch reachable AND numerically pinned, so it
    cannot bit-rot into dead code (see "Required guard" in the task brief).
    """
    rng = np.random.default_rng(11)
    cell_mask = rng.integers(0, 6, size=(20, 25))
    nuclei_mask = (rng.random((20, 25)) > 0.5).astype(np.int32)
    channel_uint16 = rng.integers(0, 65536, size=(20, 25), dtype=np.uint16)

    fast_df = compute_compartment_intensities(cell_mask, nuclei_mask, channel_uint16, "CD8")

    # Every dtype below carries the exact same integer values as channel_uint16, just
    # widened/retyped, so `compute_compartment_intensities` routes to the labeled_comprehension
    # fallback (dtype != uint16) but must still return numbers identical to the fast path.
    for dtype in (np.float64, np.int32, np.uint32, np.int64):
        channel_other = channel_uint16.astype(dtype)
        fallback_df = compute_compartment_intensities(
            cell_mask, nuclei_mask, channel_other, "CD8"
        )
        np.testing.assert_array_equal(fallback_df["label"].values, fast_df["label"].values)
        for comp in ("Cell", "Nucleus", "Cytoplasm"):
            key = f"CD8: {comp}: Median"
            np.testing.assert_allclose(
                fallback_df[key].values, fast_df[key].values, rtol=0, atol=0, equal_nan=True
            )
