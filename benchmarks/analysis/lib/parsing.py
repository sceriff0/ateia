"""Parse Nextflow trace.txt human-readable values to numbers.

Lifted and consolidated from notebooks/resource_regression.ipynb +
notebooks/resources.ipynb (parse_to_gb, parse_duration), with millisecond
handling harmonised from notebooks/sweep_analysis.ipynb.
"""
from __future__ import annotations

import re

_MEM_UNITS = {"B": 1 / 2**30, "KB": 1 / 2**20, "MB": 1 / 2**10, "GB": 1.0, "TB": 2**10}


def parse_to_gb(val) -> float:
    """'1.2 GB' / '512 MB' / '1073741824 B' / '0' -> gibibytes. '-'/'' -> NaN."""
    if val is None:
        return float("nan")
    s = str(val).strip()
    if s in ("", "-", "0"):
        return 0.0 if s == "0" else float("nan")
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)$", s, re.IGNORECASE)
    if not m:
        # bare number => assume bytes
        try:
            return float(s) / 2**30
        except ValueError:
            return float("nan")
    num, unit = float(m.group(1)), m.group(2).upper()
    return num * _MEM_UNITS[unit]


def parse_duration(val) -> float:
    """'1h 30m 45s' / '2m 3s' / '100ms' / '1.5s' -> seconds. '-'/'' -> NaN."""
    if val is None:
        return float("nan")
    s = str(val).strip()
    if s in ("", "-"):
        return float("nan")
    # milliseconds first (avoid 'm' of 'ms' matching minutes)
    total = 0.0
    matched = False
    ms = re.search(r"([\d.]+)\s*ms", s)
    if ms:
        total += float(ms.group(1)) / 1000.0
        s = s[: ms.start()] + s[ms.end():]
        matched = True
    for value, unit in re.findall(r"([\d.]+)\s*([hms])", s):
        matched = True
        total += float(value) * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit]
    return total if matched else float("nan")
