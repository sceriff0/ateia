"""What the run cost, and whether it costs the same twice.

The <=8 GB-per-process claim is STARE's entire reason to exist, so this module
MEASURES it rather than asserting it. ``exceeds_8gb`` is reported as a plain
boolean on every trace so a breach cannot hide inside an aggregate.

``measure`` INCLUDES SUBPROCESS CHILDREN, not just the calling process.
``measure``'s one real consumer, ``run_unit.py``, wraps a driver that does all
of its actual work in subprocesses -- shelling out to ``tiled_coarse.py``,
``tiled_reg_tile.py`` and ``tiled_solve.py`` via ``subprocess.run`` -- so
``RUSAGE_SELF`` alone would report only the footprint of a thin orchestrator
that does nothing but spawn children, while the registration actually
consuming gigabytes stays invisible. That would be wrong in the most
convincing direction: too small, on the exact number this module exists to
report honestly. ``RUSAGE_CHILDREN`` reports the maximum RSS over
TERMINATED, WAITED-FOR children, so this is a high-water mark across
sequential subprocesses and can UNDER-report a concurrent fan-out.
``from_trace`` is the authority at the mid and gigapixel rungs, where the
<=8 GB claim is actually adjudicated against real Nextflow task accounting.

Determinism matters for a second reason specific to this repo: a green run does
not imply a complete output tree, because conf/modules.config's errorStrategy
has an 'ignore' branch that drops a failed task without failing the run. Two
runs that produce different numbers are the symptom.
"""

from __future__ import annotations

import resource
import time

import pandas as pd

__all__ = ["measure", "from_trace", "determinism", "MEMORY_CLAIM_GB"]

MEMORY_CLAIM_GB = 8.0


def _peak_rss_gb():
    """Peak RSS across this process AND any subprocess children it awaited.

    See the module docstring for why children must be included and for the
    ``RUSAGE_CHILDREN`` under-reporting caveat on concurrent fan-outs.
    """
    self_max = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children_max = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    usage = max(self_max, children_max)
    # Linux reports KiB, macOS reports bytes.
    import sys

    return usage / (2**20) if sys.platform.startswith("linux") else usage / (2**30)


def measure(fn, *args, **kwargs):
    """Run ``fn`` and report wall time, CPU time and peak RSS.

    Both ``cpu_s`` and ``peak_rss_gb`` include subprocess children ``fn`` ran
    and waited for (e.g. via ``subprocess.run``), not just the calling
    process -- see the module docstring.
    """
    t0 = time.perf_counter()
    c0 = time.process_time()
    child0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = fn(*args, **kwargs)
    child1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu_s = (child1.ru_utime + child1.ru_stime) - (
        child0.ru_utime + child0.ru_stime
    )
    return result, {
        "wall_s": time.perf_counter() - t0,
        "cpu_s": (time.process_time() - c0) + child_cpu_s,
        "peak_rss_gb": _peak_rss_gb(),
    }


def from_trace(trace_df):
    """Aggregate a Nextflow trace frame from analysis.lib.load.parse_trace.

    ``task_seconds_sum`` is the sum of every task's ``realtime_s``. For a
    parallel fan-out this double-counts overlapped time, so it reads nearer
    serial CPU-time than elapsed wall-clock -- it is NOT ``wall_s``, despite
    a superficial resemblance.

    ``wall_s`` is the real elapsed wall-clock, computed from the trace's
    ``start_ts``/``complete_ts`` timestamp columns (added by ``parse_trace``
    only when the underlying trace file carried timestamps). When those
    columns are absent, ``wall_s`` is ``float("nan")`` -- a visibly missing
    number, not a silent fallback to ``task_seconds_sum``, which would
    reintroduce the double-counting this split exists to avoid.
    """
    peak = trace_df["peak_rss_gb"].astype(float)
    realtime = trace_df["realtime_s"].astype(float)
    cpus = trace_df["cpus"].astype(float)
    max_peak = float(peak.max())

    if "start_ts" in trace_df.columns and "complete_ts" in trace_df.columns:
        start = trace_df["start_ts"].min()
        complete = trace_df["complete_ts"].max()
        if pd.isna(start) or pd.isna(complete):
            wall_s = float("nan")
        else:
            wall_s = float((complete - start).total_seconds())
    else:
        wall_s = float("nan")

    return {
        "wall_s": wall_s,
        "task_seconds_sum": float(realtime.sum()),
        "max_peak_rss_gb": max_peak,
        "peak_rss_gb": max_peak,
        "cpu_hours": float((realtime * cpus).sum() / 3600.0),
        "n_tasks": int(len(trace_df)),
        "exceeds_8gb": bool(max_peak > MEMORY_CLAIM_GB),
    }


def determinism(a, b, keys, rtol=0.0):
    """Which of ``keys`` differ between two runs' metric dicts."""
    differing = []
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if rtol and isinstance(va, float) and isinstance(vb, float):
            same = abs(va - vb) <= rtol * max(abs(va), abs(vb), 1e-12)
        else:
            same = va == vb
        if not same:
            differing.append(k)
    return {"identical": not differing, "differing": differing}
