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


def test_from_config_defaults_to_synthetic_when_no_block_is_given():
    # The unit rung and CI must stay hermetic without naming a source.
    from benchmarks.stare_bench.texture import from_config
    assert from_config({}).kind == "synthetic"
    assert from_config({"crop_source": {"kind": "synthetic"}}).kind == "synthetic"


def test_institutional_without_paths_refuses_rather_than_falling_back():
    """The single most consequential guard in this module.

    A silent degrade to SyntheticCropSource would publish the paper's
    HEADLINE numbers -- measured on generated nuclei -- under a label
    claiming institutional tissue. It would not crash, would not fail a
    test, and would read as a completed run.
    """
    from benchmarks.stare_bench.texture import from_config
    with pytest.raises(ValueError, match="Refusing to fall back"):
        from_config({"crop_source": {"kind": "institutional"}})
    with pytest.raises(ValueError, match="Refusing to fall back"):
        from_config({"crop_source": {"kind": "institutional", "paths": []}})


def test_unknown_crop_source_kind_is_rejected():
    from benchmarks.stare_bench.texture import from_config
    with pytest.raises(ValueError, match="not one of"):
        from_config({"crop_source": {"kind": "hallucinated"}})


def test_a_missing_slide_path_fails_at_construction_not_at_first_crop(tmp_path):
    """TiffCropSource picks its slide FROM THE SEED, so a missing path would
    otherwise fail a seed-dependent subset of the plan -- scattered rows,
    hundreds deep into a multi-day sweep, long after the run looked healthy.
    """
    from benchmarks.stare_bench.texture import TiffCropSource, from_config
    ghost = tmp_path / "not_there.tiff"
    with pytest.raises(FileNotFoundError, match="do not exist"):
        TiffCropSource([str(ghost)])
    with pytest.raises(FileNotFoundError, match="do not exist"):
        from_config({"crop_source": {"kind": "institutional",
                                     "paths": [str(ghost)]}})


def test_a_glob_matching_nothing_is_an_error_not_an_empty_slide_set(tmp_path):
    # A typo'd cluster path is how "no slides" gets in; TiffCropSource would
    # report that as the wrong error ("needs at least one slide path").
    from benchmarks.stare_bench.texture import from_config
    with pytest.raises(ValueError, match="matched no files"):
        from_config({"crop_source": {"kind": "institutional",
                                     "paths": [str(tmp_path / "*.tiff")]}})


def test_resolved_paths_are_sorted_and_deduplicated(tmp_path):
    """param_hash must not depend on how the slide list was TYPED. Two configs
    naming the same slides in a different order are the same experiment.
    """
    import tifffile

    from benchmarks.stare_bench.texture import describe, from_config
    made = []
    for name in ("b", "a"):
        q = tmp_path / f"{name}.tiff"
        tifffile.imwrite(q, np.zeros((8, 8), dtype=np.uint16))
        made.append(str(q))

    forward = from_config({"crop_source": {"kind": "institutional",
                                           "paths": made}})
    backward = from_config({"crop_source": {"kind": "institutional",
                                            "paths": list(reversed(made)) + made}})
    assert describe(forward)["paths"] == describe(backward)["paths"]
    assert describe(forward)["paths"] == sorted(made)


def test_validate_rejects_a_slide_too_small_for_the_committed_size(tmp_path):
    """Header-only check, run once before the first pair. `crop` itself would
    catch this too -- but only after materialising a whole slide, on whichever
    seed happened to select it.
    """
    import tifffile

    from benchmarks.stare_bench.texture import SyntheticCropSource, TiffCropSource
    small = tmp_path / "small.tiff"
    tifffile.imwrite(small, np.zeros((64, 64), dtype=np.uint16))
    src = TiffCropSource([str(small)])

    src.validate((32, 32))  # fits: no raise
    with pytest.raises(ValueError, match="smaller than the requested"):
        src.validate((128, 128))

    # The generated source has nothing to check and must never block a run.
    assert SyntheticCropSource().validate((20480, 20480)) is None
