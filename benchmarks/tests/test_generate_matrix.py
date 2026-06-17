import numpy as np
import pytest
from benchmarks.generate_matrix import compute_target_shape, synthesize_channels


def test_compute_target_shape_scales_long_edge_preserving_aspect():
    # source 4000x2000 (HxW), target long edge 1000 -> 1000x500
    assert compute_target_shape((4000, 2000), 1000) == (1000, 500)


def test_compute_target_shape_long_edge_is_height_when_taller():
    assert compute_target_shape((2000, 4000), 1000) == (500, 1000)


def test_synthesize_channels_returns_requested_count_and_shape():
    src = np.full((8, 8), 100, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=4, seed=0)
    assert out.shape == (4, 8, 8)
    assert out.dtype == np.uint8


def test_synthesize_channels_first_channel_is_source_unchanged():
    src = np.arange(16, dtype=np.uint8).reshape(4, 4)
    out = synthesize_channels(src, n_channels=3, seed=0)
    np.testing.assert_array_equal(out[0], src)


def test_synthesize_channels_extra_channels_differ_from_source():
    src = np.full((16, 16), 120, dtype=np.uint8)
    out = synthesize_channels(src, n_channels=2, seed=0)
    assert not np.array_equal(out[1], src)  # jitter+noise+offset perturbs it


def test_synthesize_channels_is_deterministic_for_seed():
    src = np.full((16, 16), 120, dtype=np.uint8)
    a = synthesize_channels(src, n_channels=3, seed=7)
    b = synthesize_channels(src, n_channels=3, seed=7)
    np.testing.assert_array_equal(a, b)
