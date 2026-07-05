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


def test_max_pixels_skips_large_slides_without_reading(tmp_path):
    # Bug C: single-resolution slides OOM at 65536. With max_pixels set, a too-large slide is skipped
    # via the cheap shape reader — the full-array reader is NEVER called for it.
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))
    read_calls = []
    def reader(path):
        read_calls.append(path); return np.zeros((3, 4), np.uint16)
    shape_reader = lambda path: (65536, 65536)     # pretend it's huge
    res = compare_registered_dirs(a, b, atol=0.0, reader=reader,
                                  max_pixels=8192 * 8192, shape_reader=shape_reader)
    assert len(res) == 1 and res[0].get("skipped_too_large") is True
    assert res[0]["within_atol"] is True           # a skip is NOT a parity failure
    assert read_calls == []                         # never loaded the (huge) array


def test_stream_falls_back_to_full_read_when_zarr_missing(tmp_path):
    # stream=True uses zarr strip-reading on the cluster; if zarr isn't importable it must fall back to
    # the full-array reader and still produce correct drift (never silently skip).
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))
    base = np.zeros((3, 4), np.uint16); drift = base.copy(); drift[0, 0] = 7
    reader = lambda path: base if "run0000" in str(path) else drift
    res = compare_registered_dirs(a, b, atol=0.0, reader=reader, stream=True)  # zarr absent locally
    assert len(res) == 1 and res[0]["max_abs_delta"] == 7.0 and res[0]["within_atol"] is False


def test_unreadable_slide_marked_pending_not_crash(tmp_path):
    # A slide that exists in the tree but can't be read yet (mid-publish on a LIVE sweep, or corrupt) must
    # NOT abort the comparison — it is recorded as pending (equal=None, within_atol=True: not a failure).
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))
    def reader(path):
        raise OSError("truncated file — still being written")
    res = compare_registered_dirs(a, b, atol=0.0, reader=reader)
    assert len(res) == 1
    assert res[0].get("pending") is True
    assert res[0]["equal"] is None and res[0]["within_atol"] is True   # pending != parity failure


def test_one_pending_slide_does_not_block_the_other(tmp_path):
    # Two slides; one is mid-write (reader raises), the other is complete. The complete one must still be
    # compared — a single in-flight file can't blind the whole pair.
    a = _make_run(tmp_path, "run0000", slides=("P001_ref", "P001_mov1"))
    b = _make_run(tmp_path, "run0046", slides=("P001_ref", "P001_mov1"))
    good = np.arange(12, dtype=np.uint16).reshape(3, 4)
    def reader(path):
        if "P001_mov1" in str(path):
            raise OSError("still writing")
        return good
    res = {r["slide"]: r for r in compare_registered_dirs(a, b, atol=0.0, reader=reader)}
    assert res["P001_mov1"].get("pending") is True
    assert res["P001_ref"].get("pending") is None and res["P001_ref"]["equal"] is True


def test_main_reports_provisional_coverage_when_a_pair_is_pending(tmp_path, capsys):
    # End-to-end main(): one separated pair fully published (→ measured PASS) and a second whose
    # distributed run hasn't published slides yet (→ pending). The verdict must be flagged PROVISIONAL and
    # the coverage line must show 1/2, so a mid-sweep run can't be mistaken for the complete gate.
    import pandas as pd
    import tifffile
    from benchmarks.registration_eval.compare_registered import main
    plan = tmp_path / "plan.csv"
    pd.DataFrame({
        "run_id": ["cA", "sepA", "cB", "sepB"],
        "target_px": [4096, 4096, 8192, 8192], "n_channels": [2, 2, 2, 2],
        "n_register_images": [2, 2, 2, 2],
        "reg_distributed_tiling": [False, True, False, True],
        "reg_dist_force_tiling": [False, False, False, False],
        "reg_dist_tile_wh": [512] * 4, "reg_dist_tile_buffer": [100] * 4,
    }).to_csv(plan, index=False)
    img = np.arange(12, dtype=np.uint16).reshape(3, 4)
    for rid in ("cA", "sepA", "cB"):                       # cB present but sepB has NO slides -> pending pair
        out = _make_run(tmp_path, rid, slides=("P001_mov1",))
        tifffile.imwrite(out / "P001" / "registered" / "registered_slides" / "P001_mov1_registered.ome.tiff", img)
    _make_run(tmp_path, "sepB", slides=())                 # run dir exists, nothing published yet
    rc = main(["--results-root", str(tmp_path), "--run-plan", str(plan)])
    txt = capsys.readouterr().out
    assert "1/2" in txt and "PROVISIONAL" in txt and "pending" in txt
    assert "SEPARATED PARITY (must be bit-identical): PASS" in txt
    assert rc == 0                                          # the measured pair passed


def test_shape_mismatch_is_not_equal(tmp_path):
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))

    def reader(path):
        return (np.zeros((3, 4), np.uint16) if "run0000" in str(path)
                else np.zeros((3, 5), np.uint16))
    res = compare_registered_dirs(a, b, reader=reader)
    assert res[0]["equal"] is False and res[0]["within_atol"] is False
