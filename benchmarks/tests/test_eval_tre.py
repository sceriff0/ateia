import json

import numpy as np

from benchmarks.registration_eval.landmarks import LandmarkPair
from benchmarks.registration_eval import eval_tre


class _FakeSlide:
    def warp_xy(self, xy, non_rigid=True):
        # pretend perfect registration: identity warp
        return np.asarray(xy, dtype=float)


class _FakeRegistrar:
    def __init__(self):
        self.slide_dict = {"moving": _FakeSlide()}


def test_warp_moving_landmarks_uses_named_slide():
    reg = _FakeRegistrar()
    out = eval_tre.warp_moving_landmarks(reg, "moving", np.array([[1.0, 2.0]]))
    np.testing.assert_allclose(out, [[1.0, 2.0]])


def test_evaluate_pair_perfect_registration_gives_known_tre():
    # target offset by (3,4) from moving; identity warp -> TRE 5 everywhere
    pair = LandmarkPair(
        moving_xy=np.array([[0.0, 0.0], [10.0, 10.0]]),
        target_xy=np.array([[3.0, 4.0], [13.0, 14.0]]),
        pair_id="p1",
    )
    summary, tre = eval_tre.evaluate_pair(
        pair, pickle_path="ignored", slide_name="moving",
        width=100, height=0, pixel_size_um=2.0,
        loader=lambda _p: _FakeRegistrar(),
    )
    np.testing.assert_allclose(tre, [5.0, 5.0])
    assert summary["median_px"] == 5.0
    assert summary["median_rtre"] == 5.0 / 100.0
    assert summary["median_um"] == 10.0


def test_build_eval_record_merges_three_estimates():
    rec = eval_tre.build_eval_record(
        pair_id="p1", mode="tiled",
        tre_summary={"median_px": 5.0, "median_rtre": 0.05},
        valis_rtre=0.06, feature_estimate={"median": 4.2},
    )
    assert rec["pair_id"] == "p1" and rec["mode"] == "tiled"
    assert rec["true_tre"]["median_px"] == 5.0
    assert rec["valis_rtre"] == 0.06
    assert rec["feature_estimate"]["median"] == 4.2


def test_read_valis_rtre_picks_non_rigid_then_rigid(tmp_path):
    p = tmp_path / "s_summary.csv"
    p.write_text("rigid_rTRE,non_rigid_rTRE\n0.09,0.04\n")
    assert eval_tre.read_valis_rtre(p) == 0.04
    p.write_text("rigid_rTRE,non_rigid_rTRE\n0.09,\n")
    assert eval_tre.read_valis_rtre(p) == 0.09


def test_read_feature_estimate_extracts_after_registration(tmp_path):
    p = tmp_path / "fd.json"
    p.write_text(json.dumps({"after_registration": {"feature_distances": {"median": 3.3}}}))
    assert eval_tre.read_feature_estimate(p) == {"median": 3.3}
