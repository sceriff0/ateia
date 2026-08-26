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


def test_run_stare_produces_a_manifest_and_controls(pair, tmp_path):
    pair_dir, _ = pair
    got = run_stare(pair_dir, tmp_path, tile=256, halo=64, upsample=10,
                    max_error=0.99)
    assert got["manifest"].exists()
    assert len(got["controls"]) == 16
    assert all("error" in c and "mov_fg" in c for c in got["controls"])


def test_predictor_recovers_the_injected_displacement(pair, tmp_path):
    pair_dir, truth = pair
    got = run_stare(pair_dir, tmp_path, tile=256, halo=64, upsample=10,
                    max_error=0.99)
    predict = predict_from_manifest(got["manifest"], "mov")

    # A dense grid over the image, not two hand-picked points: the amplitude
    # of the injected field bounds the true displacement magnitude at roughly
    # sqrt(2) * amplitude_px, so an absolute-error assertion alone cannot tell
    # "STARE recovered the warp" from "STARE did nothing" -- the identity
    # (zero-displacement) baseline scores inside a loose absolute bound too.
    # The comparative assertion below is the one that carries meaning.
    h, w = truth["size"]
    gx, gy = np.meshgrid(np.linspace(32, w - 32, 16), np.linspace(32, h - 32, 16))
    xy = np.column_stack([gx.ravel(), gy.ravel()])

    truth_disp = _truth_disp(truth, xy)
    err_stare = np.linalg.norm(predict(xy) - truth_disp, axis=1)
    err_identity = np.linalg.norm(truth_disp, axis=1)

    ratio = err_stare.max() / err_identity.max()
    assert ratio <= 0.5, (
        f"STARE did not halve the identity baseline's worst-case error: "
        f"stare max={err_stare.max():.3f} identity max={err_identity.max():.3f} "
        f"ratio={ratio:.3f}"
    )
    # Generous absolute sanity bound retained alongside the ratio, which is
    # the assertion that actually carries meaning.
    assert err_stare.max() < 25.0


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
