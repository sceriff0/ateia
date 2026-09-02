#!/usr/bin/env python3
"""Merge per-patient CSE JSONs into one CSV (one row per patient)."""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "utils"))
from logger import configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)


def flatten(doc):
    row = {
        "id": doc["id"],
        "QualityScore": doc.get("QualityScore"),
        # Per-patient downsampling drifts QualityScore (coarser grid = different
        # metric values); carry both through so the merged report can show it
        # instead of silently discarding it. Older per-patient JSONs (written
        # before seg_quality_eval.py started emitting these) lack the keys, so
        # default to None rather than raising.
        "downsample_factor": doc.get("downsample_factor"),
        "effective_pixel_size_um": doc.get("effective_pixel_size_um"),
    }
    for k, v in doc.get("metrics", {}).items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                row[f"{k}::{kk}"] = vv
    return row


def main():
    """CLI entry point: merge per-patient CSE JSONs into one CSV (one row per patient)."""
    configure_logging(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = []
    for p in a.inputs:
        with open(p) as fh:
            data = json.load(fh)
        rows.append(flatten(data))
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    logger.info("Merged %d per-patient CSE JSON(s) into %s", len(rows), a.out)


if __name__ == "__main__":
    raise SystemExit(main())
