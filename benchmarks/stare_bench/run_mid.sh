#!/usr/bin/env bash
# Mid-rung runner: generate each synthetic pair once, then register it with
# every method through the REAL Nextflow pipeline.
#
# WHY THE REAL PIPELINE HERE and not the in-process unit driver
# (benchmarks/stare_bench/run_unit.py): the <=8 GB-per-process and fan-out
# claims only mean something under real resource limits and real task
# boundaries. Both paths call the same bin/tiled_*.py entry points, so
# neither can drift from the other.
#
# It deliberately does NOT invoke the old single-task registration entry
# point removed from bin/ when STARE became a four-stage fan-out. That script
# exists on no branch; benchmarks/registration_eval/run_registration.sh still
# calls it, which is why that harness's STARE leg has been broken since the
# split.
#
# CORRECTIONS applied against the original brief (controller rulings, see
# task-3.2-brief.md history):
#   1. The plan's amplitude_px axis is passed through to generate_pair's
#      field_params -- dropping it would silently collapse two experimental
#      conditions (12.0px / 48.0px) into one, and pair_id would then lie
#      about its own contents.
#   2. The config's committed `physics:` block (photobleach, noise_and_psf,
#      background) is read and merged with the per-row blank_regions
#      fraction. Applying ONLY blank_regions would generate clean,
#      noiseless, unbleached images and measure a problem nobody has.
#   3. lazy_images is never passed (the size-only path is not used). This
#      rung is 20480^2 (419 Mpx): banded _warp keeps generation memory-safe,
#      and field.npy is skipped above DENSE_MAX_PIXELS (64 Mpx)
#      automatically -- ground truth is recovered parametrically from
#      truth.json's field_params_call. These pairs need real images.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <plan.csv> <pairs_root> <results_root> [config.yaml]" >&2
  exit 2
fi

PLAN="$1"
PAIRS_ROOT="$2"
RESULTS_ROOT="$3"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${4:-$REPO_ROOT/benchmarks/configs/synthetic_gt.yaml}"

mkdir -p "$PAIRS_ROOT" "$RESULTS_ROOT"

# --- Step 1: generate every distinct pair ONCE ------------------------------
# Every method must see identical pixels or the comparison is meaningless.
# amplitude_px is threaded through field_params (correction 1); the config's
# physics block is read and merged with the per-row blank fraction
# (correction 2); lazy_images is never set (correction 3).
python3 - "$PLAN" "$PAIRS_ROOT" "$CONFIG" <<'PY'
import csv
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from benchmarks.stare_bench.generate import generate_pair
from benchmarks.stare_bench.texture import SyntheticCropSource

plan_csv, pairs_root, config_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

config = yaml.safe_load(config_path.read_text())
physics = config.get("physics", {})

seen = set()
with open(plan_csv, newline="") as fh:
    for row in csv.DictReader(fh):
        pid = row["pair_id"]
        if pid in seen:
            continue
        seen.add(pid)
        out = pairs_root / pid
        if (out / "truth.json").exists():
            print(f"skip {pid} (already generated)")
            continue

        physics_params = {k: dict(v) for k, v in physics.items()}
        physics_params["blank_regions"] = {"fraction": float(row["blank_fraction"])}

        generate_pair(
            out,
            json.loads(row["size"]),
            seed=int(row["seed"]),
            field_family=row["field_family"],
            field_params={
                "correlation_px": float(row["correlation_px"]),
                "amplitude_px": float(row["amplitude_px"]),
            },
            physics_params=physics_params,
            crop_source=SyntheticCropSource(),
            tile=2048,
        )

        # Registration entry-point samplesheet (lib/ParamUtils.groovy's
        # 'registration' step: patient_id,preprocessed_image,is_reference,
        # channels). Every pair is single-channel synthetic DAPI texture.
        with open(out / "samplesheet.csv", "w", newline="") as sf:
            w = csv.writer(sf)
            w.writerow(["patient_id", "preprocessed_image", "is_reference", "channels"])
            w.writerow([pid, str((out / "ref.ome.tiff").resolve()), "true", "DAPI"])
            w.writerow([pid, str((out / "mov.ome.tiff").resolve()), "false", "DAPI"])

        print(f"generated {pid}")
PY

# --- Step 2: register + score each (pair, method) through the real pipeline -
# Pipeline defaults, not the unit rung's small ones: tile 2048, halo 256
# (benchmarks/stare_bench/cli.py's own argparse defaults already match).
while IFS=, read -r run_id pair_id method rest; do
  [[ "$run_id" == "run_id" ]] && continue
  outdir="$RESULTS_ROOT/$run_id"
  mkdir -p "$outdir"
  echo ">>> $run_id (pair=$pair_id method=$method)"

  nextflow -q run "$REPO_ROOT" \
    -profile singularity \
    --input "$PAIRS_ROOT/$pair_id/samplesheet.csv" \
    --outdir "$outdir" \
    --registration_method "$method" \
    --start registration --stop registration \
    --reg_qc 2 \
    -with-trace "$outdir/trace.txt"

  python3 -m benchmarks.stare_bench.cli \
    --pair-dir "$PAIRS_ROOT/$pair_id" \
    --work-dir "$outdir/work" \
    --run-id "$run_id" \
    --method "$method" \
    --tile 2048 \
    --halo 256 \
    --out "$outdir/registration_synthetic_gt.csv"
done < "$PLAN"
