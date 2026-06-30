"""Expand a sweep.yaml into a flat run plan (one row per pipeline launch)."""
from __future__ import annotations

import argparse
import csv
import itertools
from pathlib import Path


def build_run_plan(sweep: dict) -> list[dict]:
    strategy = sweep.get("strategy", "ofat")
    axes = sweep["axes"]
    if strategy == "grid":
        keys = list(axes)
        rows = []
        for i, combo in enumerate(itertools.product(*(axes[k] for k in keys))):
            row = dict(zip(keys, combo))
            row["run_id"] = f"run{i:04d}"
            row["varied_axis"] = "grid"
            rows.append(row)
        return rows

    # ofat: one baseline run, then one run per non-baseline value of each axis
    baseline = sweep["baseline"]
    rows = [dict(baseline, run_id="run0000", varied_axis="baseline")]
    i = 1
    for axis, values in axes.items():
        for v in values:
            if v == baseline.get(axis):
                continue
            row = dict(baseline)
            row[axis] = v
            row["run_id"] = f"run{i:04d}"
            row["varied_axis"] = axis
            rows.append(row)
            i += 1
    return rows


def main():
    import yaml

    ap = argparse.ArgumentParser(description="Expand sweep.yaml into run_plan.csv")
    ap.add_argument("--sweep", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    sweep = yaml.safe_load(a.sweep.read_text())
    plan = build_run_plan(sweep)
    fields = ["run_id", "varied_axis"] + [k for k in plan[0] if k not in ("run_id", "varied_axis")]
    with open(a.out, "w", newline="") as fh:
        # lineterminator='\n' (not csv's default '\r\n') so run_sweep.sh's bash column
        # parsing doesn't see a trailing '\r' on the last field of each row.
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(plan)
    print(f"Wrote {len(plan)} runs to {a.out}")


if __name__ == "__main__":
    main()
