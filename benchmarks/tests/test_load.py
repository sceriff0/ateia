from pathlib import Path

import numpy as np
import pytest

from benchmarks.analysis.lib import load

FIX = Path(__file__).parent / "fixtures"


def test_parse_trace_converts_units():
    df = load.parse_trace(FIX / "runs/run0000/trace/trace.txt")
    seg = df[df["process"] == "SEGMENT"].iloc[0]
    assert seg["peak_rss_gb"] == pytest.approx(20.0)
    assert seg["realtime_s"] == pytest.approx(270.0)  # 4m 30s
    assert seg["cpus"] == 2


def test_parse_size_logs_input_gb_per_process():
    s = load.parse_size_logs(FIX / "runs/run0000/out/size_logs/input_sizes.csv")
    # single task per process in this fixture: per-task input == the single value
    assert s.loc["SEGMENT", "input_gb"] == pytest.approx(4.0)
    assert s.loc["CONVERT_IMAGE", "input_gb"] == pytest.approx(2.0)


def test_parse_size_logs_averages_multi_task_process(tmp_path):
    """A multi-task process (e.g. per-channel PREPROCESS) must report per-task
    input (mean), NOT the run-total sum — otherwise x is inflated by the task
    count and the memory regression is biased."""
    csv = tmp_path / "input_sizes.csv"
    csv.write_text(
        "process,sample_id,filename,bytes\n"
        "PREPROCESS,pt1,c0.ome.tif,1073741824\n"   # 1 GiB
        "PREPROCESS,pt1,c1.ome.tif,3221225472\n"   # 3 GiB
        "SEGMENT,pt1,merged.ome.tif,4294967296\n"  # 4 GiB (single task)
    )
    s = load.parse_size_logs(csv)
    assert s.loc["PREPROCESS", "input_gb"] == pytest.approx(2.0)  # mean(1,3), not sum=4
    assert s.loc["SEGMENT", "input_gb"] == pytest.approx(4.0)


def test_aggregate_repeats_reports_mean_std_cv_per_config():
    import pandas as pd

    df = pd.DataFrame({
        "process": ["SEGMENT"] * 6,
        "config_id": ["cfg000"] * 3 + ["cfg001"] * 3,
        "varied_axis": ["baseline"] * 3 + ["scaling_grid"] * 3,
        "target_px": [4096] * 3 + [8192] * 3,
        "n_channels": [2] * 6,
        "peak_rss_gb": [10.0, 12.0, 14.0, 20.0, 20.0, 20.0],
        "realtime_s": [100.0, 100.0, 100.0, 200.0, 210.0, 220.0],
    })
    out = load.aggregate_repeats(df)
    assert len(out) == 2

    c0 = out[out["config_id"] == "cfg000"].iloc[0]
    assert c0["n_reps"] == 3
    assert c0["varied_axis"] == "baseline"
    assert c0["peak_rss_gb_mean"] == pytest.approx(12.0)
    assert c0["peak_rss_gb_std"] == pytest.approx(np.std([10.0, 12.0, 14.0]))  # ddof=0
    assert c0["peak_rss_gb_cv"] == pytest.approx(np.std([10.0, 12.0, 14.0]) / 12.0)

    c1 = out[out["config_id"] == "cfg001"].iloc[0]
    assert c1["peak_rss_gb_cv"] == pytest.approx(0.0)  # zero variance across reps
    assert c1["target_px"] == 8192


def test_load_runs_joins_trace_sizes_and_params():
    df = load.load_runs(
        FIX / "runs",
        run_plan_csv=FIX / "runs_run_plan.csv",
    )
    # 2 runs x 2 processes = 4 rows
    assert len(df) == 4
    assert {"run_id", "process", "peak_rss_gb", "realtime_s", "input_gb",
            "target_px", "n_channels", "varied_axis"} <= set(df.columns)
    seg1 = df[(df["run_id"] == "run0001") & (df["process"] == "SEGMENT")].iloc[0]
    assert seg1["peak_rss_gb"] == pytest.approx(40.0)
    assert seg1["input_gb"] == pytest.approx(8.0)
    assert seg1["target_px"] == 8192


def test_only_successful_drops_failed_and_aborted():
    import pandas as pd
    from benchmarks.analysis.lib.load import only_successful
    df = pd.DataFrame({
        "process": ["A", "B", "C", "D"],
        "status": ["COMPLETED", "FAILED", "CACHED", "ABORTED"],
        "exit": [0, 137, 0, 143],
        "peak_rss_gb": [10, 999, 12, 500],
    })
    out = only_successful(df)
    assert set(out["process"]) == {"A", "C"}          # FAILED/ABORTED dropped
    # exit-only signal (no status column) also works
    df2 = pd.DataFrame({"process": ["A", "B"], "exit": [0, 1], "peak_rss_gb": [10, 999]})
    assert list(only_successful(df2)["process"]) == ["A"]


def test_load_runs_reads_the_ARM_layout_too(tmp_path):
    """run_arms.sh publishes --outdir straight to <root>/<arm>, so its size logs
    land at <root>/<arm>/size_logs/ — not the sweep's <root>/<run_id>/out/size_logs/.

    This is the bug this test exists for: load_runs hardcoded the `out/` segment,
    so every arm run loaded with input_gb = NaN. Nothing raised — the trace still
    parsed, so the frame looked complete and only the regression (which needs
    input_gb) came out empty. The arm layout is not free to change: <arm>/<patient>
    IS ihc_method's consumer contract.
    """
    import shutil
    root = tmp_path / "arm_results"
    for arm in ("valis_high_micro2", "tiled_defaults"):
        (root / arm).mkdir(parents=True)
        shutil.copytree(FIX / "runs" / "run0000" / "trace", root / arm / "trace")
        # arm layout: NO intervening out/
        shutil.copytree(FIX / "runs" / "run0000" / "out" / "size_logs",
                        root / arm / "size_logs")
    plan = tmp_path / "arm_plan.csv"
    plan.write_text("run_id,arm_kind,arm\n"
                    "valis_high_micro2,registration,valis_high_micro2\n"
                    "tiled_defaults,registration,tiled_defaults\n")

    df = load.load_runs(root, plan)
    assert len(df) == 4, "2 arms x 2 processes"
    assert df["input_gb"].notna().all(), (
        "input_gb is NaN — the size logs were not found, so the resource "
        "regression would silently fit on nothing")
    assert set(df["run_id"]) == {"valis_high_micro2", "tiled_defaults"}
