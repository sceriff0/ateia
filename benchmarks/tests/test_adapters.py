from pathlib import Path

import numpy as np

from benchmarks.registration_eval.adapters import acrobat, anhir

FIX = Path(__file__).parent / "fixtures"


def test_anhir_load_pair_reads_xy_and_aligns():
    lp = anhir.load_pair(FIX / "anhir_source.csv", FIX / "anhir_target.csv", pair_id="a")
    assert lp.pair_id == "a"
    np.testing.assert_allclose(lp.moving_xy, [[10, 20], [30, 40]])
    np.testing.assert_allclose(lp.target_xy, [[11, 19], [29, 41]])


def test_anhir_mismatched_lengths_raise():
    import pytest
    # The fixture pair has EQUAL lengths, so it cannot exercise the mismatch guard;
    # the guard is called directly with a deliberately bad pair instead. (A `short =
    # FIX / "anhir_source.csv"` binding used to sit here, unused, implying a fixture
    # that does not exist.)
    with pytest.raises(ValueError):
        anhir._validate_aligned(np.zeros((3, 2)), np.zeros((2, 2)))


def test_acrobat_load_pairs_averages_annotators():
    pairs = acrobat.load_pairs(FIX / "acrobat_landmarks.csv")
    assert len(pairs) == 1
    lp = pairs[0]
    assert lp.pair_id == "pairA"
    # annotator-averaged moving (IHC) point 0: (101, 199); point 1: (301, 399)
    np.testing.assert_allclose(lp.moving_xy, [[101, 199], [301, 399]])
    np.testing.assert_allclose(lp.target_xy, [[106, 194], [296, 404]])
    assert lp.meta["mpp"] == 1.0


def test_pairs_manifest_row_builds_expected_fields():
    from benchmarks.registration_eval.prepare_pairs import build_manifest_row
    row = build_manifest_row(
        pair_id="P1", ref_image="/d/ref.tif", moving_image="/d/mov.tif",
        source_landmarks="/d/mov.csv", target_landmarks="/d/ref.csv",
        width=1000, height=800, pixel_size_um=0.5,
    )
    assert row == {
        "pair_id": "P1", "ref_image": "/d/ref.tif", "moving_image": "/d/mov.tif",
        "source_landmarks": "/d/mov.csv", "target_landmarks": "/d/ref.csv",
        "width": 1000, "height": 800, "pixel_size_um": 0.5,
    }
