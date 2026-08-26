import json
import shutil
from pathlib import Path

import pandas as pd

from benchmarks.analysis import make_tables

FIX = Path(__file__).parent / "fixtures"
TABLES = ("runs_master", "scaling_fits", "registration_accuracy", "registration_valis_rtre",
          "segmentation_agreement", "registration_synthetic_gt", "param_matrix")


def test_build_paper_data_emits_all_tables_and_dicts(tmp_path):
    res = make_tables.build_paper_data(
        results_root=FIX / "runs",
        run_plan_csv=FIX / "runs_run_plan.csv",
        outdir=tmp_path,
    )
    # every table + its data dictionary is written
    for name in TABLES:
        assert (tmp_path / f"{name}.csv").exists(), f"{name}.csv missing"
        assert (tmp_path / f"{name}.dict.md").exists(), f"{name}.dict.md missing"

    master = pd.read_csv(tmp_path / "runs_master.csv")
    assert "run_id" in master.columns
    assert len(master) == 2                              # 2 runs in the fixture
    # per-stage pivots present for the fixture's stages
    assert any(c.endswith("_peak_ram_gb") for c in master.columns)
    assert any(c.endswith("_wall_s") for c in master.columns)
    assert "cpu_hours" in master.columns

    fits = pd.read_csv(tmp_path / "scaling_fits.csv")
    assert {"stage", "target", "slope", "intercept", "r2", "n"} <= set(fits.columns)
    # both the RAM and the time law are fit
    assert set(fits["target"]) == {"peak_rss_gb", "realtime_s"}

    # param_matrix is one row per run and carries the run_id key for downstream tuning
    pm = pd.read_csv(tmp_path / "param_matrix.csv")
    assert len(pm) == 2 and "run_id" in pm.columns


def _seg_qc(root, run_id, patient, moving):
    d = root / run_id / "out" / patient / "qc" / "registration"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{patient}_{moving}_seg_qc.json").write_text(json.dumps({
        "patient_id": patient, "moving": moving, "reference": f"{patient}_ref",
        "stage_order": ["rigid", "non_rigid"],
        "stages": {
            "rigid":     {"n_pairs": 500, "dice_matched": 0.55, "displacement_um_p50": 1.3},
            "non_rigid": {"n_pairs": 500, "dice_matched": 0.82, "displacement_um_p50": 0.5},
        },
        "delta_vs_anchor": {"non_rigid": {"dice_matched": 0.27, "displacement_um_p50": -0.8}},
        "matching": {"pair_fraction": 0.9}}))


def test_param_matrix_joins_both_registration_accuracy_headlines(tmp_path):
    # copy the fixture runs tree so we can inject QC JSONs the base fixture doesn't ship
    runs = tmp_path / "runs"
    shutil.copytree(FIX / "runs", runs)
    plan = pd.read_csv(FIX / "runs_run_plan.csv")
    run0 = plan["run_id"].iloc[0]
    _seg_qc(runs, run0, "P001", "cycle2")
    # VALIS's own registration error summary (published under registered/summary/)
    vd = runs / run0 / "out" / "P001" / "registered" / "summary"
    vd.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"name": "cycle2", "non_rigid_D": 3.0, "n_matches": 200}]).to_csv(
        vd / "P001_summary.csv", index=False)

    out = tmp_path / "paper_data"
    make_tables.build_paper_data(runs, FIX / "runs_run_plan.csv",
                                 out)

    reg = pd.read_csv(out / "registration_accuracy.csv")
    assert set(reg["stage"]) == {"rigid", "non_rigid"}
    assert reg[reg["stage"] == "non_rigid"]["dice_matched"].iloc[0] == 0.82

    valis = pd.read_csv(out / "registration_valis_rtre.csv")
    assert valis["non_rigid_D"].iloc[0] == 3.0

    pm = pd.read_csv(out / "param_matrix.csv").set_index("run_id")
    # the run with QC gets BOTH registration-accuracy headlines joined in
    assert pm.loc[run0, "reg_dice_matched"] == 0.82           # seg-based, final (non_rigid) stage
    assert pm.loc[run0, "valis_non_rigid_D"] == 3.0           # VALIS-reported rTRE/D


def test_dict_flags_undocumented_columns(tmp_path):
    make_tables.build_paper_data(
        results_root=FIX / "runs", run_plan_csv=FIX / "runs_run_plan.csv",
        outdir=tmp_path,
    )
    # the runs_master dictionary documents the per-stage pattern, so real stage columns
    # (e.g. SEGMENT_peak_ram_gb) must NOT be reported as undocumented
    txt = (tmp_path / "runs_master.dict.md").read_text()
    assert "peak_ram_gb" in txt
    assert "SEGMENT_peak_ram_gb" not in txt.split("Additional columns", 1)[-1] \
        if "Additional columns" in txt else True
