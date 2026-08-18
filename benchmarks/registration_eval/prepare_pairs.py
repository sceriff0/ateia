"""Organize a downloaded ANHIR/ACROBAT dataset into per-pair input dirs for register.py.

For each pair, creates <out>/<pair_id>/input/ containing symlinks to the reference
and moving images (register.py reads a directory), and writes pairs_manifest.csv.
Landmark files are referenced (not moved) for the evaluator.

Data is account-gated for both challenges — see README.md. This script does NOT
download; point --data-dir at your local copy.
"""
from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


def build_manifest_row(pair_id, ref_image, moving_image, source_landmarks,
                       target_landmarks, width, height, pixel_size_um) -> dict:
    return {
        "pair_id": pair_id,
        "ref_image": str(ref_image),
        "moving_image": str(moving_image),
        "source_landmarks": str(source_landmarks),
        "target_landmarks": str(target_landmarks),
        "width": int(width),
        "height": int(height),
        "pixel_size_um": pixel_size_um,
    }


def _link_input_dir(out_root: Path, pair_id: str, ref_image: Path, moving_image: Path) -> Path:
    d = out_root / pair_id / "input"
    d.mkdir(parents=True, exist_ok=True)
    for img in (ref_image, moving_image):
        dst = d / Path(img).name
        if not dst.exists():
            os.symlink(os.path.abspath(img), dst)
    return d


def write_manifest(rows, out_csv: Path) -> None:
    fields = ["pair_id", "ref_image", "moving_image", "source_landmarks",
              "target_landmarks", "width", "height", "pixel_size_um"]
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Prepare challenge pairs for registration eval.")
    ap.add_argument("--pairs-csv", required=True,
                    help="user-provided CSV describing pairs: pair_id,ref_image,moving_image,"
                         "source_landmarks,target_landmarks,width,height,pixel_size_um")
    ap.add_argument("--out", required=True, help="output root for per-pair input dirs + manifest")
    a = ap.parse_args()

    out_root = Path(a.out)
    out_root.mkdir(parents=True, exist_ok=True)
    rows = []
    with open(a.pairs_csv) as fh:
        for r in csv.DictReader(fh):
            _link_input_dir(out_root, r["pair_id"], Path(r["ref_image"]), Path(r["moving_image"]))
            rows.append(build_manifest_row(
                r["pair_id"], r["ref_image"], r["moving_image"],
                r["source_landmarks"], r["target_landmarks"],
                r["width"], r["height"], float(r["pixel_size_um"]) if r.get("pixel_size_um") else None,
            ))
    write_manifest(rows, out_root / "pairs_manifest.csv")
    print(f"Prepared {len(rows)} pairs under {out_root}")


if __name__ == "__main__":
    main()
