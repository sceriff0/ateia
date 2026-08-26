import numpy as np
import pytest

from benchmarks.stare_bench.texture import (
    CropSource,
    SyntheticCropSource,
    describe,
)


def test_synthetic_source_is_deterministic():
    src = SyntheticCropSource()
    a = src.crop((128, 128), seed=4)
    b = src.crop((128, 128), seed=4)
    np.testing.assert_array_equal(a, b)


def test_crop_shape_dtype_and_range():
    got = SyntheticCropSource().crop((64, 96), seed=1)
    assert got.shape == (64, 96)
    assert got.dtype == np.float32
    assert got.min() >= 0.0 and got.max() <= 1.0


def test_synthetic_source_actually_has_nuclei():
    # A flat crop would silently make every registration trivially easy, so the
    # seed set must carry real structure.
    got = SyntheticCropSource().crop((256, 256), seed=2)
    assert got.std() > 0.02


def test_describe_records_provenance_for_truth_json():
    meta = describe(SyntheticCropSource())
    assert meta["kind"] == "synthetic"
    assert meta["releasable"] is True


def test_cropsource_is_abstract():
    with pytest.raises(TypeError):
        CropSource()
