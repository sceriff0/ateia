"""What the run cost, and whether it costs the same twice.

The <=8 GB-per-process claim is STARE's entire reason to exist, so this module
MEASURES it rather than asserting it. ``exceeds_8gb`` is reported as a plain
boolean on every trace so a breach cannot hide inside an aggregate.

Determinism matters for a second reason specific to this repo: a green run does
not imply a complete output tree, because conf/modules.config's errorStrategy
has an 'ignore' branch that drops a failed task without failing the run. Two
runs that produce different numbers are the symptom.
"""

from __future__ import annotations

import resource
import time

__all__ = ["measure", "from_trace", "determinism", "MEMORY_CLAIM_GB"]

MEMORY_CLAIM_GB = 8.0


def _peak_rss_gb():
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS reports bytes.
    import sys

    return usage / (2**20) if sys.platform.startswith("linux") else usage / (2**30)


def measure(fn, *args, **kwargs):
    """Run ``fn`` and report wall time, CPU time and peak RSS."""
    t0 = time.perf_counter()
    c0 = time.process_time()
    result = fn(*args, **kwargs)
    return result, {
        "wall_s": time.perf_counter() - t0,
        "cpu_s": time.process_time() - c0,
        "peak_rss_gb": _peak_rss_gb(),
    }


def from_trace(trace_df):
    """Aggregate a Nextflow trace frame from analysis.lib.load.parse_trace."""
    peak = trace_df["peak_rss_gb"].astype(float)
    realtime = trace_df["realtime_s"].astype(float)
    cpus = trace_df["cpus"].astype(float)
    max_peak = float(peak.max())
    return {
        "wall_s": float(realtime.sum()),
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
