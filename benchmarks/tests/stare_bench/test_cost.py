import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.stare_bench.metrics.cost import determinism, from_trace, measure

REPO_ROOT = Path(__file__).resolve().parents[3]


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
    # convincing direction: too small. A child allocation must move
    # peak_rss_gb materially above a no-child baseline.
    #
    # THE CHILD ALLOCATES 1 GB, not the ~300 MB it used to. That margin was too
    # thin to survive a different platform, and this test had never actually run
    # in CI to prove otherwise -- the job died during collection from 2026-08-26
    # until the dependency fix, so its first real execution FAILED at
    # peak=0.3117 vs baseline=0.1748, a 0.137 delta against a 0.2 threshold.
    #
    # Nothing was wrong with measure(): the child WAS counted (0.31 > 0.17). The
    # arithmetic was just marginal. The parent's own baseline is ~179 MB because
    # cost.py imports pandas, while a 300 MB child totals only ~319 MB -- so the
    # delta is the difference of two similar numbers, and which one wins depends
    # on interpreter and library footprints that differ per platform.
    #
    # Raising the allocation makes the signal dominate that noise instead of
    # competing with it, and STRENGTHENS the assertion: a 1 GB child that failed
    # to register would now show a delta near zero rather than merely a small
    # one. The threshold goes up with it, so this is not a loosened test.
    #
    # ru_maxrss (RUSAGE_SELF and RUSAGE_CHILDREN alike) is a LIFETIME
    # high-water mark that never resets within a process. Comparing a
    # "no-child" baseline against a "spawned a child" measurement is only
    # meaningful in a FRESH interpreter: inside the shared pytest process,
    # whichever heavy test ran earlier (test_generate.py rendering images,
    # test_field_quality.py/test_epe.py building rasters) permanently raises
    # the high-water mark, so a 300 MB child can no longer move it and the
    # delta collapses to zero -- a failure that is a function of suite
    # execution order, not of the code. Spawning a throwaway interpreter
    # makes the assertion a property of measure() itself.
    script = textwrap.dedent(
        """
        import json
        import subprocess
        import sys

        from benchmarks.stare_bench.metrics.cost import measure

        baseline = measure(lambda: None)[1]["peak_rss_gb"]

        def spawn_child():
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import numpy as np; a = np.ones(1_000_000_000 // 8); "
                    "print(a.sum())",
                ],
                check=True,
                capture_output=True,
            )

        _, stats = measure(spawn_child)
        print(json.dumps({"baseline": baseline, "peak": stats["peak_rss_gb"]}))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["peak"] > result["baseline"] + 0.5, result


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


def test_from_trace_reports_exceeds_8gb_as_unmeasured_on_an_empty_trace():
    # On an empty frame, peak.max() is NaN and bool(nan > 8.0) is False --
    # a compliance claim ("did not exceed 8 GB") derived from zero measured
    # tasks. exceeds_8gb must come back None (unmeasured), not a fabricated
    # False.
    df = pd.DataFrame({
        "process": pd.Series([], dtype=str),
        "peak_rss_gb": pd.Series([], dtype=float),
        "realtime_s": pd.Series([], dtype=float),
        "cpus": pd.Series([], dtype=float),
    })
    got = from_trace(df)
    assert got["exceeds_8gb"] is None


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
