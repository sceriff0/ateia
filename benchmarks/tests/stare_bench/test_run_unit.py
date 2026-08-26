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
    xy = np.array([[512.0, 512.0], [300.0, 700.0]])
    err = np.linalg.norm(predict(xy) - _truth_disp(truth, xy), axis=1)
    assert err.max() < 25.0, f"STARE did not recover the warp: {err}"


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
