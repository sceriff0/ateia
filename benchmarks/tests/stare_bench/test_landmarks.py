import numpy as np
import pytest

from benchmarks.stare_bench.landmarks import (
    LandmarkPair,
    image_diagonal,
    per_landmark_tre,
    summarize,
)


def test_per_landmark_tre_euclidean():
    warped = np.array([[0.0, 0.0], [3.0, 0.0]])
    target = np.array([[0.0, 0.0], [0.0, 4.0]])
    tre = per_landmark_tre(warped, target)
    np.testing.assert_allclose(tre, [0.0, 5.0])


def test_per_landmark_tre_shape_mismatch_raises():
    with pytest.raises(ValueError):
        per_landmark_tre(np.zeros((3, 2)), np.zeros((2, 2)))


def test_image_diagonal():
    assert image_diagonal(3, 4) == pytest.approx(5.0)


def test_summarize_px_and_rtre():
    tre = np.array([0.0, 10.0])  # median 5, mean 5
    s = summarize(tre, diagonal=100.0)
    assert s["n"] == 2
    assert s["median_px"] == pytest.approx(5.0)
    assert s["mean_px"] == pytest.approx(5.0)
    assert s["median_rtre"] == pytest.approx(0.05)
    assert s["p90_px"] == pytest.approx(9.0)  # numpy linear interp on [0,10]


def test_summarize_microns_when_pixel_size_given():
    tre = np.array([2.0, 2.0])
    s = summarize(tre, diagonal=100.0, pixel_size_um=0.5)
    assert s["median_um"] == pytest.approx(1.0)
    assert s["p90_um"] == pytest.approx(1.0)


def test_summarize_omits_microns_without_pixel_size():
    s = summarize(np.array([1.0]), diagonal=10.0)
    assert "median_um" not in s


def test_landmarkpair_holds_arrays():
    lp = LandmarkPair(moving_xy=np.zeros((2, 2)), target_xy=np.ones((2, 2)), pair_id="p1")
    assert lp.pair_id == "p1" and lp.moving_xy.shape == (2, 2)
