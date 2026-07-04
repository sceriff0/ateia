import json

import numpy as np
import pandas as pd

from benchmarks.analysis.lib import quality


# ── cost ──
def test_run_cost_summary_cpu_gpu_hours_and_bottleneck():
    df = pd.DataFrame([
        {"process": "MIRAGE:PREPROCESSING:PREPROCESS", "realtime_s": 100, "cpus": 4, "run_id": "r0", "varied_axis": "baseline"},
        {"process": "MIRAGE:REGISTRATION:VALIS_ADAPTER:REGISTER", "realtime_s": 400, "cpus": 8, "run_id": "r0", "varied_axis": "baseline"},
        {"process": "MIRAGE:POSTPROCESSING:SEGMENT", "realtime_s": 200, "cpus": 2, "run_id": "r0", "varied_axis": "baseline"},
    ])
    out = quality.run_cost_summary(df).set_index("run_id")
    row = out.loc["r0"]
    assert row["total_realtime_s"] == 700
    assert row["cpu_hours"] == (100*4 + 400*8 + 200*2) / 3600
    assert row["gpu_hours"] == 200 / 3600           # only SEGMENT is a GPU leaf
    assert row["bottleneck_stage"] == "REGISTER"     # largest single realtime
    assert abs(row["bottleneck_frac"] - 400/700) < 1e-9


def test_run_cost_summary_wall_clock_from_timestamps():
    ts = pd.to_datetime(["2026-07-01 10:00:00", "2026-07-01 10:02:00"])
    tc = pd.to_datetime(["2026-07-01 10:01:00", "2026-07-01 10:07:00"])
    df = pd.DataFrame({"process": ["A:P", "A:REGISTER"], "realtime_s": [60, 300], "cpus": [1, 1],
                       "run_id": ["r0", "r0"], "start_ts": ts, "complete_ts": tc})
    out = quality.run_cost_summary(df).set_index("run_id")
    assert out.loc["r0", "wall_clock_s"] == 7 * 60   # 10:00:00 start -> 10:07:00 complete


# ── registration accuracy ──
def _write_fd(root, run_id, patient, stem, after_median, after_mean, improv):
    d = root / run_id / "out" / patient / "feature_distances"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}_feature_distances.json").write_text(json.dumps({
        "after_registration": {"feature_distances": {"median": after_median, "mean": after_mean}},
        "improvement": {"distance_reduction_percent": improv}}))


def test_harvest_registration_error(tmp_path):
    plan = tmp_path / "plan.csv"
    pd.DataFrame({"run_id": ["r0", "r1"]}).to_csv(plan, index=False)
    _write_fd(tmp_path, "r0", "P001", "P001_mov1_DAPI", 1.2, 1.5, 88.0)
    _write_fd(tmp_path, "r0", "P001", "P001_mov2_DAPI", 1.4, 1.7, 90.0)   # 2 moving images -> aggregate
    # r1 has no feature_distances -> skipped
    out = quality.harvest_registration_error(tmp_path, plan).set_index("run_id")
    assert list(out.index) == ["r0"]
    assert out.loc["r0", "reg_tre_median_px"] == np.median([1.2, 1.4])
    assert out.loc["r0", "reg_improvement_pct"] == np.mean([88.0, 90.0])


def test_harvest_registration_error_tolerates_bad_json(tmp_path):
    plan = tmp_path / "plan.csv"; pd.DataFrame({"run_id": ["r0"]}).to_csv(plan, index=False)
    d = tmp_path / "r0" / "out" / "P001" / "feature_distances"; d.mkdir(parents=True)
    (d / "x_feature_distances.json").write_text("{ not json")
    assert quality.harvest_registration_error(tmp_path, plan).empty   # no crash, just empty


# ── segmentation counts + agreement (injected mask reader) ──
def _mk_seg_run(tmp_path, run_id, patient="P001"):
    d = tmp_path / run_id / "out" / patient / "segment"; d.mkdir(parents=True, exist_ok=True)
    p = d / f"{patient}_cell_mask.tif"; p.write_bytes(b""); return p


def test_harvest_segmentation_counts(tmp_path):
    plan = tmp_path / "plan.csv"; pd.DataFrame({"run_id": ["r0", "r1"]}).to_csv(plan, index=False)
    _mk_seg_run(tmp_path, "r0"); _mk_seg_run(tmp_path, "r1")
    masks = {"r0": np.array([[0, 1], [2, 3]]), "r1": np.array([[0, 0], [0, 5]])}  # max label = cell count
    reader = lambda p: masks["r0" if "r0" in str(p) else "r1"]
    out = quality.harvest_segmentation_counts(tmp_path, plan, reader=reader).set_index("run_id")
    assert out.loc["r0", "n_cells"] == 3 and out.loc["r1", "n_cells"] == 5


def test_segmentation_agreement_pairwise(tmp_path):
    plan = tmp_path / "plan.csv"
    pd.DataFrame({"run_id": ["s", "c"], "target_px": [4096, 4096], "n_channels": [2, 2],
                  "seg_method": ["stardist", "cellsam"]}).to_csv(plan, index=False)
    _mk_seg_run(tmp_path, "s"); _mk_seg_run(tmp_path, "c")
    # contiguous labels 1..N (as real masks are relabeled), so max label == cell count
    m = {"s": np.array([[1, 1], [0, 2]]), "c": np.array([[1, 0], [0, 2]])}   # 2 vs 2 cells, partial overlap
    reader = lambda p: m["s" if "/s/" in str(p) else "c"]
    out = quality.segmentation_agreement(tmp_path, plan, reader=reader)
    assert len(out) == 1
    r = out.iloc[0]
    assert {r["method_a"], r["method_b"]} == {"stardist", "cellsam"}
    # foreground: s = {(0,0),(0,1),(1,1)}=3px, c = {(0,0),(1,1)}=2px, inter=2, union=3
    assert abs(r["foreground_iou"] - 2/3) < 1e-9
    assert r["n_cells_a"] == 2 and r["n_cells_b"] == 2
