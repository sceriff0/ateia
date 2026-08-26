import subprocess
import sys

import numpy as np
import pandas as pd

from benchmarks.stare_bench.metrics.cost import determinism, from_trace, measure


def test_measure_reports_wall_time_and_memory():
    def work():
        return np.ones((256, 256)).sum()

    result, stats = measure(work)
    assert result == 256 * 256
    assert stats["wall_s"] >= 0.0
    assert stats["peak_rss_gb"] > 0.0


def test_measure_reports_subprocess_children_not_just_self():
    # measure()'s one real consumer (run_unit.py) does all its actual
    # registration work in subprocesses via subprocess.run. RUSAGE_SELF alone
    # sees only the calling orchestrator and would report the footprint of a
    # process that does nothing but spawn children -- wrong in the most
    # convincing direction: too small. A modest (~300 MB) child allocation
    # must move peak_rss_gb materially above a no-child baseline.
    baseline = measure(lambda: None)[1]["peak_rss_gb"]

    def spawn_child():
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import numpy as np; a = np.ones(300_000_000 // 8); print(a.sum())",
            ],
            check=True,
            capture_output=True,
        )

    _, stats = measure(spawn_child)
    assert stats["peak_rss_gb"] > baseline + 0.2


def test_from_trace_aggregates_the_nextflow_columns():
    df = pd.DataFrame({
        "process": ["TILED_REG_TILE", "TILED_REG_TILE", "TILED_STITCH"],
        "peak_rss_gb": [2.0, 7.5, 4.0],
        "realtime_s": [10.0, 20.0, 30.0],
        "cpus": [2, 2, 4],
    })
    got = from_trace(df)
    assert got["max_peak_rss_gb"] == 7.5
    assert got["n_tasks"] == 3
    # (10*2 + 20*2 + 30*4) / 3600
    assert got["cpu_hours"] == (20 + 40 + 120) / 3600
    # task_seconds_sum double-counts overlapped time on purpose -- it is the
    # sum of realtime_s, not a wall-clock.
    assert got["task_seconds_sum"] == 10.0 + 20.0 + 30.0
    # No start_ts/complete_ts columns (parse_trace only adds them if the
    # underlying trace carried timestamps) -- wall_s must be NaN, not a
    # silent fallback to the sum.
    assert np.isnan(got["wall_s"])


def test_from_trace_computes_real_wall_clock_from_timestamps_when_present():
    df = pd.DataFrame({
        "process": ["TILED_REG_TILE", "TILED_REG_TILE"],
        "peak_rss_gb": [2.0, 3.0],
        "realtime_s": [10.0, 10.0],
        "cpus": [2, 2],
        "start_ts": pd.to_datetime(["2026-01-01T00:00:00", "2026-01-01T00:00:02"]),
        "complete_ts": pd.to_datetime(["2026-01-01T00:00:10", "2026-01-01T00:00:12"]),
    })
    got = from_trace(df)
    # Two overlapping 10s tasks starting 2s apart span 12s end-to-end, not the
    # 20s task_seconds_sum would suggest.
    assert got["wall_s"] == 12.0
    assert got["task_seconds_sum"] == 20.0


def test_from_trace_flags_a_breach_of_the_eight_gb_claim():
    df = pd.DataFrame({
        "process": ["TILED_REG_TILE"],
        "peak_rss_gb": [9.1],
        "realtime_s": [5.0],
        "cpus": [1],
    })
    assert from_trace(df)["exceeds_8gb"] is True


def test_determinism_detects_a_changed_value():
    a = {"mean_px": 1.0, "median_px": 2.0}
    b = {"mean_px": 1.0, "median_px": 2.5}
    got = determinism(a, b, ["mean_px", "median_px"])
    assert got["identical"] is False
    assert got["differing"] == ["median_px"]


def test_determinism_passes_on_identical_runs():
    a = {"mean_px": 1.0}
    got = determinism(a, dict(a), ["mean_px"])
    assert got["identical"] is True
    assert got["differing"] == []
