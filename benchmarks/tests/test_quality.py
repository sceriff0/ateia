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


# ── registration accuracy (staged QC, reg_qc=2) ──
def _write_seg_qc(root, run_id, patient, moving, stages, deltas, pair_fraction=0.9):
    d = root / run_id / "out" / patient / "qc" / "registration"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{patient}_{moving}_seg_qc.json").write_text(json.dumps({
        "patient_id": patient, "moving": moving, "reference": f"{patient}_ref",
        "stages_separable": True, "stage_order": list(stages),
        "stages": stages, "delta_vs_anchor": deltas,
        "matching": {"pair_fraction": pair_fraction, "n_pairs": 1000}}))


def test_harvest_registration_qc(tmp_path):
    plan = tmp_path / "plan.csv"
    pd.DataFrame({"run_id": ["r0", "r1"]}).to_csv(plan, index=False)
    stages = {
        "rigid":     {"n_pairs": 1000, "iou_mean": 0.42, "iou_p50": 0.40,
                      "dice_matched": 0.55, "displacement_px_p50": 4.1, "displacement_um_p50": 1.33},
        "non_rigid": {"n_pairs": 1000, "iou_mean": 0.71, "iou_p50": 0.70,
                      "dice_matched": 0.80, "displacement_px_p50": 1.6, "displacement_um_p50": 0.52},
    }
    deltas = {"non_rigid": {"dice_matched": 0.25, "displacement_um_p50": -0.81,
                            "displacement_px_p50": -2.5}}
    _write_seg_qc(tmp_path, "r0", "P001", "cycle2", stages, deltas, pair_fraction=0.91)
    # r1 has no seg_qc -> skipped
    out = quality.harvest_registration_qc(tmp_path, plan)
    assert set(out["run_id"]) == {"r0"}
    assert set(out["stage"]) == {"rigid", "non_rigid"}
    nr = out[out["stage"] == "non_rigid"].iloc[0]
    assert nr["dice_matched"] == 0.80
    assert nr["displacement_um_p50"] == 0.52
    assert nr["delta_dice_vs_rigid"] == 0.25
    assert nr["pair_fraction"] == 0.91
    rigid = out[out["stage"] == "rigid"].iloc[0]
    assert np.isnan(rigid["delta_dice_vs_rigid"])   # anchor has no delta


def test_harvest_registration_qc_tolerates_bad_json(tmp_path):
    plan = tmp_path / "plan.csv"; pd.DataFrame({"run_id": ["r0"]}).to_csv(plan, index=False)
    d = tmp_path / "r0" / "out" / "P001" / "qc" / "registration"; d.mkdir(parents=True)
    (d / "x_seg_qc.json").write_text("{ not json")
    assert quality.harvest_registration_qc(tmp_path, plan).empty   # no crash, just empty


# ── registration accuracy (VALIS-reported rTRE / D) ──
def _write_valis_summary(root, run_id, patient, rows):
    d = root / run_id / "out" / patient / "registered" / "summary"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(d / f"{patient}_summary.csv", index=False)


def test_harvest_valis_rtre_and_per_run(tmp_path):
    plan = tmp_path / "plan.csv"
    pd.DataFrame({"run_id": ["r0", "r1"]}).to_csv(plan, index=False)
    _write_valis_summary(tmp_path, "r0", "P001", [
        {"name": "cycle1", "original_D": 40.0, "rigid_D": 8.0, "non_rigid_D": 3.0, "n_matches": 200},
        {"name": "cycle2", "original_D": 44.0, "rigid_D": 9.0, "non_rigid_D": 5.0, "n_matches": 180},
    ])
    # r1 has no summary -> contributes nothing
    long = quality.harvest_valis_rtre(tmp_path, plan)
    assert set(long["run_id"]) == {"r0"}
    assert len(long) == 2                                   # two slides
    assert "non_rigid_D" in long.columns and "summary_csv" in long.columns

    per_run = quality.valis_rtre_per_run(long).set_index("run_id")
    # median across the two slides, prefixed valis_
    assert per_run.loc["r0", "valis_non_rigid_D"] == 4.0    # median(3, 5)
    assert per_run.loc["r0", "valis_original_D"] == 42.0     # median(40, 44)


def test_harvest_valis_rtre_tolerates_missing(tmp_path):
    plan = tmp_path / "plan.csv"; pd.DataFrame({"run_id": ["r0"]}).to_csv(plan, index=False)
    assert quality.harvest_valis_rtre(tmp_path, plan).empty
    assert quality.valis_rtre_per_run(pd.DataFrame(columns=["run_id"])).empty


# ── segmentation quality (CellSegmentationEvaluator, reference-free) ──
def _write_seg_eval(root, run_id, patient, quality_score, cell_metrics):
    d = root / run_id / "out" / patient / "qc" / "segmentation"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{patient}_seg_eval.json").write_text(json.dumps({
        "id": patient,
        "metrics": {"Matched Cell": cell_metrics, "QualityScore": quality_score},
        "QualityScore": quality_score}))


def test_harvest_seg_quality(tmp_path):
    plan = tmp_path / "plan.csv"
    pd.DataFrame({"run_id": ["r0", "r1"]}).to_csv(plan, index=False)
    _write_seg_eval(tmp_path, "r0", "P001", 0.87,
                    {"NumberOfCellsPer100SquareMicrons": 12.3,
                     "FractionOfForegroundOccupiedByCells": 0.64})
    # r1 has no seg_eval -> skipped
    out = quality.harvest_seg_quality(tmp_path, plan).set_index("run_id")
    assert list(out.index) == ["r0"]
    assert out.loc["r0", "QualityScore"] == 0.87
    assert out.loc["r0", "patient"] == "P001"
    assert out.loc["r0", "cell:NumberOfCellsPer100SquareMicrons"] == 12.3
    assert out.loc["r0", "cell:FractionOfForegroundOccupiedByCells"] == 0.64


# ── segmentation counts + agreement (injected mask reader) ──
def _mk_seg_run(tmp_path, run_id, patient="P001"):
    d = tmp_path / run_id / "out" / patient / "segment"; d.mkdir(parents=True, exist_ok=True)
    p = d / f"{patient}_cell_mask.tif"; p.write_bytes(b""); return p


def test_harvest_segmentation_counts(tmp_path):
    plan = tmp_path / "plan.csv"; pd.DataFrame({"run_id": ["r0", "r1"]}).to_csv(plan, index=False)
    _mk_seg_run(tmp_path, "r0"); _mk_seg_run(tmp_path, "r1")
    # r1 has a NON-contiguous label (5) for a single cell — distinct-count = 1 (correct); max-label = 5.
    masks = {"r0": np.array([[0, 1], [2, 3]]), "r1": np.array([[0, 0], [0, 5]])}
    reader = lambda p: masks["r0" if "r0" in str(p) else "r1"]
    out = quality.harvest_segmentation_counts(tmp_path, plan, reader=reader).set_index("run_id")
    assert out.loc["r0", "n_cells"] == 3 and out.loc["r1", "n_cells"] == 1


def test_instance_f1_iou_matching():
    ma = np.array([[1, 1, 1], [2, 2, 2]])
    mb = np.array([[1, 1, 1], [0, 0, 2]])   # cell1 exact match (IoU=1); cell2 IoU=1/3 < 0.5 -> unmatched
    r = quality.instance_f1(ma, mb, iou_thresh=0.5)
    assert r["n_a"] == 2 and r["n_b"] == 2 and r["matched"] == 1
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["f1"] == 0.5
    # identical masks -> perfect agreement
    r2 = quality.instance_f1(ma, ma)
    assert r2["f1"] == 1.0 and r2["matched"] == 2


def test_harvest_segmentation_counts_finds_global_default_location(tmp_path):
    # SEGMENT uses the global-default publishDir -> masks land at out/segment/ (NOT out/<patient>/...),
    # so the harvest must search recursively. Regression for the wrong */segment*/ glob.
    plan = tmp_path / "plan.csv"; pd.DataFrame({"run_id": ["r0"]}).to_csv(plan, index=False)
    d = tmp_path / "r0" / "out" / "segment"; d.mkdir(parents=True)   # no patient dir
    (d / "P001_cell_mask.tif").write_bytes(b"")
    reader = lambda p: np.array([[0, 1], [2, 3]])
    out = quality.harvest_segmentation_counts(tmp_path, plan, reader=reader)
    assert len(out) == 1 and out.iloc[0]["n_cells"] == 3


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
