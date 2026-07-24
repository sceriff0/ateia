#!/usr/bin/env python3
"""
generate_resource_report.py
Self-contained HTML computational-resources report for a MIRAGE run.
Joins the aggregated input-size log with Nextflow's trace.txt. Stdlib only.
"""

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

_UNIT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_bytes(s):
    """Parse a Nextflow byte string ('3.2 GB', '512 MB', '-') to float bytes."""
    if s is None:
        return None
    s = s.strip()
    if not s or s in {"-", "0"} and s == "-":
        return None
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)$", s, re.IGNORECASE)
    if m:
        return round(float(m.group(1)) * _UNIT[m.group(2).upper()], 1)
    try:
        return float(s)
    except ValueError:
        return None


def parse_duration(s):
    """Parse a duration ('12m 4s', '2h 1m', '1.5s', '-') to float seconds."""
    if s is None:
        return None
    s = s.strip()
    if not s or s == "-":
        return None
    total = 0.0
    found = False
    for value, unit in re.findall(r"([\d.]+)\s*(ms|s|m|h|d)", s):
        found = True
        v = float(value)
        total += {"ms": v / 1000, "s": v, "m": v * 60, "h": v * 3600, "d": v * 86400}[unit]
    if found:
        return total
    try:
        return float(s)
    except ValueError:
        return None


def parse_percent(s):
    """Parse a percentage string ('142.3%', '-') to float."""
    if s is None:
        return None
    s = s.strip().rstrip("%")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_trace(path):
    """Parse trace.txt (TSV) into a list of per-task dicts with normalized fields."""
    rows = []
    if not path or not Path(path).exists():
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for r in reader:
            rows.append({
                "process": (r.get("process") or "").strip(),
                "tag": (r.get("tag") or "").strip(),
                "name": (r.get("name") or "").strip(),
                "status": (r.get("status") or "").strip(),
                "exit": (r.get("exit") or "").strip(),
                "realtime_s": parse_duration(r.get("realtime")),
                "duration_s": parse_duration(r.get("duration")),
                "cpu_pct": parse_percent(r.get("%cpu")),
                "peak_rss_b": parse_bytes(r.get("peak_rss")),
                "peak_vmem_b": parse_bytes(r.get("peak_vmem")),
                "rchar_b": parse_bytes(r.get("rchar")),
                "wchar_b": parse_bytes(r.get("wchar")),
            })
    return rows


def parse_size_log(path):
    """Sum input bytes per (process, sample_id) from input_sizes.csv."""
    out = {}
    if not path or not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            proc = (row.get("process") or "").strip()
            sample = (row.get("sample_id") or "").strip()
            if not proc or proc == "STUB":
                continue
            try:
                b = int(float(row.get("bytes") or 0))
            except ValueError:
                b = 0
            out[(proc, sample)] = out.get((proc, sample), 0) + b
    return out


def _maxf(values):
    """Max of the non-None floats, or None."""
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _sumf(values):
    """Sum of the non-None floats (0.0 if all None)."""
    return sum(v for v in values if v is not None)


def rollup_by_process(trace_rows):
    """Aggregate trace rows into per-process resource summaries."""
    groups = {}
    for r in trace_rows:
        groups.setdefault(r["process"], []).append(r)
    out = []
    for proc, rows in sorted(groups.items()):
        rts = [r.get("realtime_s") for r in rows]
        realtime_total = _sumf(rts)
        n = len(rows)
        out.append({
            "process": proc,
            "n_tasks": n,
            "realtime_total_s": realtime_total,
            "realtime_mean_s": round(realtime_total / n, 1) if n else 0.0,
            "cpu_max_pct": _maxf([r.get("cpu_pct") for r in rows]),
            "peak_rss_max_b": _maxf([r.get("peak_rss_b") for r in rows]),
            "peak_vmem_max_b": _maxf([r.get("peak_vmem_b") for r in rows]),
            "rchar_total_b": _sumf([r.get("rchar_b") for r in rows]),
            "wchar_total_b": _sumf([r.get("wchar_b") for r in rows]),
            "n_failed": sum(1 for r in rows if r.get("exit") not in ("0", "", None)),
        })
    return out


def join_size(trace_rows, size_map):
    """Attach input_bytes to each trace row via (process, tag), with prefix fallback."""
    # Pre-index sample ids by process for fallback matching.
    by_proc = {}
    for (proc, sample), b in size_map.items():
        by_proc.setdefault(proc, []).append((sample, b))
    joined = []
    for r in trace_rows:
        proc, tag = r["process"], r.get("tag", "")
        input_bytes = size_map.get((proc, tag))
        if input_bytes is None:
            # Fallback: a size sample whose id is a prefix of the trace tag
            # (trace tag is meta.id = "<patient>_<stem>"; size sample is patient).
            for sample, b in by_proc.get(proc, []):
                if sample and (tag == sample or tag.startswith(sample)):
                    input_bytes = b
                    break
        joined.append({**r, "input_bytes": input_bytes})
    return joined
