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
