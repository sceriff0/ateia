import json

import numpy as np
import tifffile

from benchmarks.stare_bench import __version__
from benchmarks.stare_bench.fields import DENSE_MAX_PIXELS, make_field
from benchmarks.stare_bench.generate import _warp, generate_pair, param_hash
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


def _bilinear_sample(img, xy):
    """Sample ``img`` at fractional ``xy`` points via bilinear interpolation.

    Same clamp-to-edge / convex-combination scheme as ``generate._warp``'s
    per-point math, used here to check real pixel CONTENT rather than
    re-deriving the algebra that produced the landmarks in the first place.
    """
    h, w = img.shape
    x = np.clip(xy[:, 0], 0, w - 1)
    y = np.clip(xy[:, 1], 0, h - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    tx, ty = x - x0, y - y0
    top = img[y0, x0] * (1 - tx) + img[y0, x1] * tx
    bot = img[y1, x0] * (1 - tx) + img[y1, x1] * tx
    return top * (1 - ty) + bot * ty


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


def test_landmarks_correspond_to_matching_image_content(tmp_path):
    """The ground-truth check that matters: target_xy sampled in ref and
    moving_xy sampled in mov must show the SAME tissue, not merely satisfy the
    field's own algebra (a tautological check cannot catch a
    swapped-direction bug because it never touches the written pixels).

    ``_warp`` pull-samples ``mov(p) = ref(p + disp(p))``, so a moving-space
    point corresponds to a ref-space point offset by the field evaluated AT
    THE MOVING POINT -- see ``_landmarks``'s docstring for why. Using
    ``physics_params={}`` isolates the warp (mov is then an exact resample of
    ref through the known field), so the only residual is bilinear
    interpolation noise.

    Measured on this fixture (seed=11, 256x256, default random_fourier
    field, physics_params={}): mean abs error ~0.000164 for the correct
    ``target = moving + field.sample(moving)`` correspondence, vs. ~0.0596
    (about 360x larger, systematic -- not noise) for the swapped
    ``moving = target + field.sample(target)`` bug. The threshold below sits
    almost two orders of magnitude above the correct value and well below the
    wrong one.
    """
    truth = _gen(tmp_path, physics_params={})
    lm = np.asarray(truth["landmarks"])
    target, moving = lm[:, 0:2], lm[:, 2:4]

    ref = tifffile.imread(tmp_path / "ref.ome.tiff").astype(np.float64)
    mov = tifffile.imread(tmp_path / "mov.ome.tiff").astype(np.float64)

    ref_vals = _bilinear_sample(ref, target)
    mov_vals = _bilinear_sample(mov, moving)
    mae = float(np.mean(np.abs(ref_vals - mov_vals)))
    assert mae < 0.01, f"landmark content mismatch: mean abs error {mae}"


def test_warp_is_invariant_to_band_size():
    """``_warp`` is processed in row bands for memory (see its docstring);
    pin that the banding is purely a memory optimisation and never changes a
    single sampled value, on a shape that does not divide evenly by the band
    height.
    """
    field = make_field("affine", (37, 53), seed=3, rotation_deg=12.0,
                       scale=1.05, translation=(2.0, -1.5))
    image = np.random.default_rng(0).random((37, 53)).astype(np.float32)
    banded = _warp(image, field, band_rows=5)
    whole = _warp(image, field, band_rows=1000)
    np.testing.assert_array_equal(banded, whole)


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


def test_dense_field_cap_is_the_real_expression_not_hardcoded(tmp_path, monkeypatch):
    """On the REAL (non-lazy) path, force ``DENSE_MAX_PIXELS`` down to
    something a 256x256 image exceeds, and confirm ``dense_field_written``
    comes out False from evaluating ``h * w <= DENSE_MAX_PIXELS`` -- not from
    a hardcoded constant, and not because ``lazy_images`` is involved (it is
    explicitly False here).
    """
    import benchmarks.stare_bench.generate as generate_module

    monkeypatch.setattr(generate_module, "DENSE_MAX_PIXELS", 10)
    truth = generate_pair(tmp_path, (256, 256), seed=11,
                          crop_source=SyntheticCropSource(), physics_params={},
                          tile=64, lazy_images=False)
    assert truth["dense_field_written"] is False
    assert not (tmp_path / "field.npy").exists()


def test_write_dense_field_override_is_honoured_on_the_real_path(tmp_path):
    forced_true = _gen(tmp_path / "forced_true", write_dense_field=True)
    forced_false = _gen(tmp_path / "forced_false", write_dense_field=False)
    assert forced_true["dense_field_written"] is True
    assert (tmp_path / "forced_true" / "field.npy").exists()
    assert forced_false["dense_field_written"] is False
    assert not (tmp_path / "forced_false" / "field.npy").exists()


def test_param_hash_changes_with_any_input():
    a = param_hash({"seed": 1, "x": 2})
    b = param_hash({"seed": 1, "x": 3})
    assert a != b and len(a) == 16
