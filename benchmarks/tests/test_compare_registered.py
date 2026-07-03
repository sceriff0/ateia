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


def test_shape_mismatch_is_not_equal(tmp_path):
    a = _make_run(tmp_path, "run0000", slides=("P001_mov1",))
    b = _make_run(tmp_path, "run0046", slides=("P001_mov1",))

    def reader(path):
        return (np.zeros((3, 4), np.uint16) if "run0000" in str(path)
                else np.zeros((3, 5), np.uint16))
    res = compare_registered_dirs(a, b, reader=reader)
    assert res[0]["equal"] is False and res[0]["within_atol"] is False
