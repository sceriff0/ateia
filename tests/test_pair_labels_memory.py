"""`pair_labels_to_reference` must not build one DataFrame row per foreground pixel.

`bin/extract_cell_properties.py:222` built

    pairs = pd.DataFrame({"lbl": flat[fg], "ref": ref[fg]})
    counts = pairs.groupby(["lbl", "ref"]).size().reset_index(name="n")

-- one row for every pixel where both masks are non-zero. That is the exact construct
`bin/quantify.py:158` warns about in its own comment: "A per-pixel pandas DataFrame (one row per
foreground pixel, rebuilt on every channel) is what caused the WSI-scale memory regression."
Quantification was fixed; this path was not, and it is live on the **default InstanSeg dual-mask
backend**, where the nucleus and cell masks carry independent label spaces and so must actually
be paired.

At 40000x40000 with ~30% tissue that is ~5x10^8 rows, before pandas' groupby machinery adds its
own copies.

The replacement packs `(label, reference)` into one integer key and uses `np.unique`, which is
the same computation without the frame.

**Tie-breaking is part of the contract, not an implementation detail.** `groupby(...).idxmax()`
returns the FIRST row of the maximum, and `counts` is ordered by `(lbl, ref)`, so a tie went to
the SMALLEST reference label. Measured against the old implementation before replacing it:
a nucleus overlapping cell 3 twice and cell 7 twice resolved to `{1: 3}`. These tests pin that,
so the rewrite cannot quietly re-key a cell.
"""

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)

np = pytest.importorskip("numpy")
pytest.importorskip("pandas")
pytest.importorskip("skimage")

import extract_cell_properties as ecp  # noqa: E402


def test_dominant_overlap_wins():
    label = np.array([[1, 1, 1], [0, 0, 0]], dtype=np.uint32)
    reference = np.array([[9, 9, 4], [0, 0, 0]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, reference) == {1: 9}


def test_a_tie_goes_to_the_smaller_reference_label():
    """Measured against the pre-rewrite implementation: {1: 3}, not {1: 7}."""
    label = np.array([[1, 1, 1, 1], [2, 2, 0, 0]], dtype=np.uint32)
    reference = np.array([[7, 7, 3, 3], [5, 5, 0, 0]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, reference) == {1: 3, 2: 5}


def test_labels_with_no_overlapping_reference_are_absent():
    """Only labels that overlap a non-zero reference get a mapping."""
    label = np.array([[1, 1], [2, 2]], dtype=np.uint32)
    reference = np.array([[5, 5], [0, 0]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, reference) == {1: 5}


def test_no_overlap_at_all_returns_empty():
    label = np.array([[1, 1]], dtype=np.uint32)
    reference = np.array([[0, 0]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, reference) == {}


def test_identity_label_spaces_resolve_to_the_identity_mapping():
    """StarDist / CellSAM share label ids between the two masks; pairing must be a no-op there."""
    label = np.array([[1, 1, 2], [3, 3, 0]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, label) == {1: 1, 2: 2, 3: 3}


def test_large_reference_label_ids_do_not_collide():
    """The packed key must not alias two (label, reference) pairs into one."""
    label = np.array([[1, 1, 2, 2]], dtype=np.uint32)
    reference = np.array([[10**6, 10**6, 3, 3]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, reference) == {1: 10**6, 2: 3}


def test_no_per_pixel_dataframe_is_built(monkeypatch):
    """The point of the change: pin it behaviourally, not by reading the source."""
    calls = []

    class Tripwire:
        def __init__(self, *a, **kw):
            calls.append((a, kw))
            raise AssertionError(
                "pair_labels_to_reference built a pandas DataFrame; at WSI scale this is one row "
                "per foreground pixel, the construct quantify.py:158 documents as the cause of "
                "the memory regression"
            )

    monkeypatch.setattr(ecp.pd, "DataFrame", Tripwire)

    label = np.array([[1, 1, 1], [2, 2, 0]], dtype=np.uint32)
    reference = np.array([[9, 9, 4], [5, 5, 0]], dtype=np.uint32)

    assert ecp.pair_labels_to_reference(label, reference) == {1: 9, 2: 5}
    assert calls == []


def test_agrees_with_a_brute_force_reference_on_random_masks():
    """A property check over seeded random masks, independent of either implementation."""
    rng = np.random.default_rng(20260816)
    label = rng.integers(0, 6, size=(64, 64)).astype(np.uint32)
    reference = rng.integers(0, 5, size=(64, 64)).astype(np.uint32)

    expected = {}
    for lab in np.unique(label):
        if lab == 0:
            continue
        refs = reference[(label == lab) & (reference != 0)]
        if refs.size == 0:
            continue
        values, counts = np.unique(refs, return_counts=True)
        best = counts.max()
        # tie -> smallest reference label, matching the documented contract
        expected[int(lab)] = int(values[counts == best].min())

    assert ecp.pair_labels_to_reference(label, reference) == expected
