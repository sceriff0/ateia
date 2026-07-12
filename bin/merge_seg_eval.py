#!/usr/bin/env python3
"""Merge per-patient CSE JSONs into one CSV (one row per patient)."""
import argparse, csv, json


def flatten(doc):
    row = {"id": doc["id"], "QualityScore": doc.get("QualityScore")}
    for k, v in doc.get("metrics", {}).items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                row[f"{k}::{kk}"] = vv
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = [flatten(json.loads(open(p).read())) for p in a.inputs]
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
