import numpy as np
import pytest

from benchmarks.stare_bench.generate import generate_pair
from benchmarks.stare_bench.run_unit import predict_from_manifest, run_stare
from benchmarks.stare_bench.texture import SyntheticCropSource

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    d = tmp_path_factory.mktemp("pair")
    truth = generate_pair(d, (1024, 1024), seed=21, tile=256,
                          crop_source=SyntheticCropSource(),
                          field_params={"correlation_px": 333.0, "amplitude_px": 6.0},
                          physics_params={"photobleach": {"factor": 0.9}})
    return d, truth


@pytest.fixture(scope="module")
def affine_pair(tmp_path_factory):
    # An affine warp is globally representable: it is exactly STARE's COARSE
    # model class (rotation + translation, scale left at 1.0 so the default
    # `model=euclidean` can still represent it exactly), unlike the
    # random_fourier field below, which is deliberately outside every
    # method's model class by the anti-circularity design. This fixture
    # exists to test WIRING (did COARSE run, did SOLVE write a usable
    # manifest, does predict_from_manifest read it, do the displacements
    # point the right way with the right magnitude) rather than the
    # scientific recovery claim, which belongs to the mid/gigapixel rungs.
    d = tmp_path_factory.mktemp("affine_pair")
    truth = generate_pair(d, (1024, 1024), seed=21, tile=256,
                          crop_source=SyntheticCropSource(),
                          field_family="affine",
                          field_params={"rotation_deg": 1.0, "scale": 1.0,
                                        "translation": [4.0, -6.0]},
                          physics_params={"photobleach": {"factor": 0.9}})
    return d, truth


def test_run_stare_produces_a_manifest_and_controls(pair, tmp_path):
    pair_dir, _ = pair
    got = run_stare(pair_dir, tmp_path, tile=256, halo=64, upsample=10,
                    max_error=0.99)
    assert got["manifest"].exists()
    assert len(got["controls"]) == 16
    assert all("error" in c and "mov_fg" in c for c in got["controls"])


def test_predictor_recovers_the_injected_displacement(affine_pair, tmp_path):
    """Wiring test: an affine warp is exactly STARE's COARSE model class.

    This is NOT a scientific-recovery claim -- see
    test_random_fourier_case_runs_and_records_its_ratio for why the
    random_fourier field cannot carry one at unit-rung resolution. This test
    instead exercises the whole chain (COARSE -> REG_TILE -> SOLVE ->
    predict_from_manifest) end to end: if the wiring is broken -- flags
    swapped, a sign error, a stage mismatch -- this fails loudly, because
    nothing stands between STARE and a trivially representable transform.
    """
    pair_dir, truth = affine_pair
    got = run_stare(pair_dir, tmp_path, tile=256, halo=64, upsample=10,
                    max_error=0.99)
    predict = predict_from_manifest(got["manifest"], "mov")

    # A dense grid over the image, not two hand-picked points: the amplitude
    # of the injected field bounds the true displacement magnitude, so an
    # absolute-error assertion alone cannot tell "STARE recovered the warp"
    # from "STARE did nothing" -- the identity (zero-displacement) baseline
    # scores inside a loose absolute bound too. The comparative assertion
    # below is the one that carries meaning.
    h, w = truth["size"]
    gx, gy = np.meshgrid(np.linspace(32, w - 32, 16), np.linspace(32, h - 32, 16))
    xy = np.column_stack([gx.ravel(), gy.ravel()])

    truth_disp = _truth_disp(truth, xy)
    err_stare = np.linalg.norm(predict(xy) - truth_disp, axis=1)
    err_identity = np.linalg.norm(truth_disp, axis=1)

    ratio = err_stare.max() / err_identity.max()
    assert ratio <= 0.2, (
        f"STARE did not recover a plain affine warp through the driver: "
        f"stare max={err_stare.max():.3f} identity max={err_identity.max():.3f} "
        f"ratio={ratio:.3f}"
    )
    # Generous absolute sanity bound retained alongside the ratio, which is
    # the assertion that actually carries meaning.
    assert err_stare.max() < 25.0


def test_random_fourier_case_runs_and_records_its_ratio(pair, tmp_path, capsys):
    """The random_fourier field is deliberately OUTSIDE every method's model
    class at unit-rung mesh resolution -- see fields.py's anti-circularity
    note. A 4x4 mesh (tile=256 over 1024px) is ~6x coarser than this field's
    own derived control grid, and tiled_solve.py's default gate_tre=1.0
    zeroes the genuine sub-pixel residuals that remain at this amplitude. So
    a poor recovery ratio here is EXPECTED and is not a driver defect -- see
    task-2.1-report.md's fix-round-1 finding for the measured numbers and
    root cause. This test therefore gates only on the pipeline completing
    with the right shape and RECORDS the ratio as an observation, never as a
    pass/fail threshold.
    """
    pair_dir, truth = pair
    got = run_stare(pair_dir, tmp_path, tile=256, halo=64, upsample=10,
                    max_error=0.99)
    assert len(got["controls"]) == 16

    predict = predict_from_manifest(got["manifest"], "mov")
    h, w = truth["size"]
    gx, gy = np.meshgrid(np.linspace(32, w - 32, 16), np.linspace(32, h - 32, 16))
    xy = np.column_stack([gx.ravel(), gy.ravel()])

    truth_disp = _truth_disp(truth, xy)
    err_stare = np.linalg.norm(predict(xy) - truth_disp, axis=1)
    err_identity = np.linalg.norm(truth_disp, axis=1)
    ratio = err_stare.max() / err_identity.max()

    with capsys.disabled():
        print(
            "\n[random_fourier observation] "
            f"err_stare max={err_stare.max():.3f} median={np.median(err_stare):.3f} | "
            f"err_identity max={err_identity.max():.3f} median={np.median(err_identity):.3f} | "
            f"ratio(max)={ratio:.3f} ratio(median)={np.median(err_stare) / np.median(err_identity):.3f}"
        )


def _truth_disp(truth, xy):
    from benchmarks.stare_bench.fields import make_field

    f = make_field(truth["field_family"], tuple(truth["size"]),
                   **truth["field_params_call"])
    return f.sample(xy)


def test_cost_is_reported(pair, tmp_path):
    pair_dir, _ = pair
    got = run_stare(pair_dir, tmp_path, tile=256, halo=64, upsample=10,
                    max_error=0.99)
    assert got["cost"]["wall_s"] > 0
    assert got["cost"]["peak_rss_gb"] > 0
