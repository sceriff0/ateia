import json

import numpy as np

from benchmarks.registration_eval import eval_tre
from benchmarks.registration_eval.landmarks import LandmarkPair


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
        pair, transform_path="ignored", slide_name="moving",
        width=100, height=0, pixel_size_um=2.0,
        loader=lambda _p: _FakeRegistrar(),
    )
    np.testing.assert_allclose(tre, [5.0, 5.0])
    assert summary["median_px"] == 5.0
    assert summary["median_rtre"] == 5.0 / 100.0
    assert summary["median_um"] == 10.0


def test_build_eval_record_merges_ground_truth_and_native_signals():
    rec = eval_tre.build_eval_record(
        pair_id="p1", mode="valis",
        tre_summary={"median_px": 5.0, "median_rtre": 0.05},
        valis_rtre=0.06,
        seg_qc={"stage": "non_rigid", "dice_matched": 0.91},
    )
    assert rec["pair_id"] == "p1" and rec["mode"] == "valis"
    assert rec["true_tre"]["median_px"] == 5.0
    assert rec["valis_rtre"] == 0.06
    assert rec["seg_qc"]["dice_matched"] == 0.91
    # a valis run has no STARE number; the key is present-and-None, never absent,
    # so the aggregated CSV keeps one stable column set across methods.
    assert rec["stare_tre"] is None


def test_read_valis_rtre_picks_non_rigid_then_rigid(tmp_path):
    p = tmp_path / "s_summary.csv"
    p.write_text("rigid_rTRE,non_rigid_rTRE\n0.09,0.04\n")
    assert eval_tre.read_valis_rtre(p) == 0.04
    p.write_text("rigid_rTRE,non_rigid_rTRE\n0.09,\n")
    assert eval_tre.read_valis_rtre(p) == 0.09


# ── STARE (tiled) path: landmark TRE through the transform manifest ──
def test_evaluate_pair_tiled_uses_injected_warper():
    pair = LandmarkPair(
        moving_xy=np.array([[0.0, 0.0], [10.0, 10.0]]),
        target_xy=np.array([[3.0, 4.0], [13.0, 14.0]]),
        pair_id="p1",
    )
    seen = {}

    def fake_warp(slide, xy, stage):
        seen["slide"], seen["stage"] = slide, stage
        return np.asarray(xy, dtype=float)  # identity -> TRE 5 everywhere

    summary, tre = eval_tre.evaluate_pair(
        pair, transform_path="manifest.json", slide_name="moving",
        width=100, height=0, pixel_size_um=2.0,
        loader=lambda _p: fake_warp, method="tiled",
    )
    np.testing.assert_allclose(tre, [5.0, 5.0])
    assert summary["median_px"] == 5.0
    # non_rigid=True (default) must select STARE's mesh-refined stage, not rigid
    assert seen == {"slide": "moving", "stage": "refined"}


def test_evaluate_pair_tiled_rigid_stage_when_non_rigid_false():
    pair = LandmarkPair(moving_xy=np.array([[0.0, 0.0]]),
                        target_xy=np.array([[0.0, 0.0]]), pair_id="p1")
    seen = {}

    def fake_warp(slide, xy, stage):
        seen["stage"] = stage
        return np.asarray(xy, dtype=float)

    eval_tre.evaluate_pair(pair, "m.json", "moving", 100, 0,
                           loader=lambda _p: fake_warp, method="tiled", non_rigid=False)
    assert seen["stage"] == "rigid"


def test_evaluate_pair_rejects_unknown_method():
    pair = LandmarkPair(moving_xy=np.array([[0.0, 0.0]]),
                        target_xy=np.array([[0.0, 0.0]]), pair_id="p1")
    try:
        eval_tre.evaluate_pair(pair, "x", "moving", 10, 10, method="ashlar")
    except ValueError as exc:
        assert "ashlar" in str(exc)
    else:
        raise AssertionError("unknown method must raise, not silently pick a default")


# ── method-native readers ──
def test_read_stare_tre_flattens_headline_fields(tmp_path):
    p = tmp_path / "m_tre.json"
    p.write_text(json.dumps({
        "coarse_tre_px": 2.0, "n_tiles": 9, "mesh_refined": True,
        "rigid_tre_px": {"p50": 3.0, "p90": 5.0},
        "residual_after_px": {"p50": 0.5, "p90": 0.9},
    }))
    out = eval_tre.read_stare_tre(p)
    assert out["coarse_tre_px"] == 2.0
    assert out["rigid_p50_px"] == 3.0
    assert out["final_p50_px"] == 0.5
    assert out["mesh_refined"] is True


def test_read_stare_tre_leaves_final_none_when_no_tile_was_refined(tmp_path):
    # no residual_after_px block -> the post-mesh number must stay None rather than
    # silently reporting the rigid number as if it were the final accuracy.
    p = tmp_path / "m_tre.json"
    p.write_text(json.dumps({"coarse_tre_px": 2.0, "mesh_refined": False,
                             "rigid_tre_px": {"p50": 3.0}}))
    out = eval_tre.read_stare_tre(p)
    assert out["rigid_p50_px"] == 3.0
    assert out["final_p50_px"] is None


def test_read_seg_qc_takes_the_final_stage(tmp_path):
    p = tmp_path / "m_seg_qc.json"
    p.write_text(json.dumps({
        "stage_order": ["native", "rigid", "non_rigid"],
        "stages": {
            "native": {"dice_matched": 0.10, "median_displacement_um": 40.0},
            "rigid": {"dice_matched": 0.80, "median_displacement_um": 3.0},
            "non_rigid": {"dice_matched": 0.92, "median_displacement_um": 0.8},
        },
        "matching": {"n_pairs": 1234},
    }))
    out = eval_tre.read_seg_qc(p)
    assert out["stage"] == "non_rigid"          # the fully-registered stage, not the first
    assert out["dice_matched"] == 0.92
    assert out["median_displacement_um"] == 0.8
    assert out["n_pairs"] == 1234


def test_read_seg_qc_returns_none_when_no_stage_scored(tmp_path):
    p = tmp_path / "m_seg_qc.json"
    p.write_text(json.dumps({"stages": {}}))
    assert eval_tre.read_seg_qc(p) is None
