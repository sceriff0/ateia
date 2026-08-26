import json

import numpy as np
import tifffile

from benchmarks.stare_bench import __version__
from benchmarks.stare_bench.fields import DENSE_MAX_PIXELS
from benchmarks.stare_bench.generate import generate_pair, param_hash
from benchmarks.stare_bench.texture import SyntheticCropSource

PHYSICS = {
    "photobleach": {"factor": 0.8},
    "blank_regions": {"fraction": 0.2},
}


def _gen(tmp_path, **kw):
    kw.setdefault("crop_source", SyntheticCropSource())
    kw.setdefault("physics_params", PHYSICS)
    kw.setdefault("tile", 64)
    return generate_pair(tmp_path, (256, 256), seed=11, **kw)


def test_writes_the_expected_artifacts(tmp_path):
    truth = _gen(tmp_path)
    assert (tmp_path / "ref.ome.tiff").exists()
    assert (tmp_path / "mov.ome.tiff").exists()
    assert (tmp_path / "truth.json").exists()
    on_disk = json.loads((tmp_path / "truth.json").read_text())
    assert on_disk["generator_version"] == __version__
    assert on_disk["param_hash"] == truth["param_hash"]


def test_is_byte_identical_under_a_fixed_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _gen(a)
    _gen(b)
    assert (a / "ref.ome.tiff").read_bytes() == (b / "ref.ome.tiff").read_bytes()
    assert (a / "mov.ome.tiff").read_bytes() == (b / "mov.ome.tiff").read_bytes()


def test_landmarks_round_trip_through_the_known_field(tmp_path):
    from benchmarks.stare_bench.fields import make_field

    truth = _gen(tmp_path)
    lm = np.asarray(truth["landmarks"])
    field = make_field(truth["field_family"], (256, 256), **truth["field_params_call"])
    moving = lm[:, 2:4]
    target = lm[:, 0:2]
    np.testing.assert_allclose(moving - field.sample(target), target, atol=1e-6)


def test_tile_labels_cover_every_tile(tmp_path):
    truth = _gen(tmp_path)
    assert len(truth["tile_labels"]) == 16  # 256/64 squared


def test_moving_image_differs_from_reference(tmp_path):
    truth = _gen(tmp_path)
    ref = tifffile.imread(tmp_path / "ref.ome.tiff")
    mov = tifffile.imread(tmp_path / "mov.ome.tiff")
    assert ref.shape == mov.shape
    assert not np.array_equal(ref, mov)
    assert truth["physics_params"] == PHYSICS


def test_dense_field_is_skipped_above_the_cap(tmp_path):
    big = int(np.sqrt(DENSE_MAX_PIXELS)) + 1000
    truth = generate_pair(tmp_path, (big, big), seed=1,
                          crop_source=SyntheticCropSource(), tile=2048,
                          physics_params={}, lazy_images=True)
    assert truth["dense_field_written"] is False


def test_param_hash_changes_with_any_input():
    a = param_hash({"seed": 1, "x": 2})
    b = param_hash({"seed": 1, "x": 3})
    assert a != b and len(a) == 16
