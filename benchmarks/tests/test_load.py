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
        manifest_csv=FIX / "runs_matrix_manifest.csv",
    )
    # 2 runs x 2 processes = 4 rows
    assert len(df) == 4
    assert {"run_id", "process", "peak_rss_gb", "realtime_s", "input_gb",
            "target_px", "n_channels", "varied_axis"} <= set(df.columns)
    seg1 = df[(df["run_id"] == "run0001") & (df["process"] == "SEGMENT")].iloc[0]
    assert seg1["peak_rss_gb"] == pytest.approx(40.0)
    assert seg1["input_gb"] == pytest.approx(8.0)
    assert seg1["target_px"] == 8192
