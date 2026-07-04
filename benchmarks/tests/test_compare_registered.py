import numpy as np

from benchmarks.registration_eval.compare_registered import (
    compare_registered_dirs, find_registered)


def _make_run(tmp_path, run_id, patient="P001", slides=("P001_ref", "P001_mov1")):
    out = tmp_path / run_id / "out"
    for s in slides:
        d = out / patient / "registered" / "registered_slides"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{s}_registered.ome.tiff").write_bytes(b"")  # content read via injected reader
    return out


def test_find_registered_maps_patient_and_stem(tmp_path):
    out = _make_run(tmp_path, "run0000")
    found = find_registered(out)
    assert set(found) == {("P001", "P001_ref"), ("P001", "P001_mov1")}


def test_identical_pixels_pass(tmp_path):
    a = _make_run(tmp_path, "run0000")
    b = _make_run(tmp_path, "run0046")
    img = {(  # same array for every slide on both sides
        p, s): np.arange(12, dtype=np.uint16).reshape(3, 4) for p in ("P001",)
        for s in ("P001_ref", "P001_mov1")}
    reader = lambda path: img[("P001", str(path).split("/")[-1].replace("_registered.ome.tiff", ""))]
    res = compare_registered_dirs(a, b, atol=0.0, reader=reader)
    assert len(res) == 2
    assert all(r["equal"] and r["within_atol"] and r["max_abs_delta"] == 0.0 for r in res)


def test_pixel_difference_fails_at_atol_zero(tmp_path):
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))
    base = np.zeros((3, 4), dtype=np.uint16)
    diff = base.copy(); diff[0, 0] = 5

    def reader(path):
        return base if "run0000" in str(path) else diff
    res = compare_registered_dirs(a, b, atol=0.0, reader=reader)
    assert len(res) == 1
    assert res[0]["equal"] is False and res[0]["within_atol"] is False
    assert res[0]["max_abs_delta"] == 5.0
    # a tolerance >= the delta passes (e.g. accept a documented float32-field epsilon)
    res2 = compare_registered_dirs(a, b, atol=5.0, reader=reader)
    assert res2[0]["within_atol"] is True


def test_drift_metrics_max_mean_and_pct(tmp_path):
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))
    base = np.zeros((3, 4), dtype=np.uint16)
    drift = base.copy(); drift[0, 0] = 5; drift[1, 1] = 3   # 2 of 12 pixels differ, max 5, mean 8/12

    def reader(path):
        return base if "run0000" in str(path) else drift
    r = compare_registered_dirs(a, b, atol=0.0, reader=reader)[0]
    assert r["max_abs_delta"] == 5.0
    assert abs(r["mean_abs_delta"] - 8/12) < 1e-9
    assert abs(r["pct_pixels_diff"] - 100*2/12) < 1e-9
    assert r["within_atol"] is False


def test_auto_pair_labels_separated_and_tiled(tmp_path):
    import pandas as pd
    from benchmarks.registration_eval.compare_registered import _auto_pair
    plan = tmp_path / "plan.csv"
    pd.DataFrame({
        "run_id": ["c", "sep", "til"], "target_px": [4096, 4096, 4096],
        "n_channels": [2, 2, 2], "n_register_images": [2, 2, 2],
        "reg_distributed_tiling": [False, True, True],
        "reg_dist_force_tiling": [False, False, True],
        "reg_dist_tile_wh": [512, 512, 256], "reg_dist_tile_buffer": [100, 100, 50],
    }).to_csv(plan, index=False)
    for r in ("c", "sep", "til"):
        _make_run(tmp_path, r)
    pairs = _auto_pair(tmp_path, plan)
    by_path = {p["path"]: p for p in pairs}
    assert set(by_path) == {"separated", "tiled"}
    assert by_path["tiled"]["tile_wh"] == 256 and by_path["tiled"]["tile_buffer"] == 50
    # both pair against the SAME classic run
    assert str(by_path["separated"]["classic_out"]).endswith("c/out")
    assert str(by_path["tiled"]["classic_out"]).endswith("c/out")


def test_shape_mismatch_is_not_equal(tmp_path):
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))

    def reader(path):
        return (np.zeros((3, 4), np.uint16) if "run0000" in str(path)
                else np.zeros((3, 5), np.uint16))
    res = compare_registered_dirs(a, b, reader=reader)
    assert res[0]["equal"] is False and res[0]["within_atol"] is False
