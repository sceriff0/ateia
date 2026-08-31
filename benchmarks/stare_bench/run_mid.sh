#!/usr/bin/env bash
# Mid-rung runner: generate each synthetic pair once, then register it with
# every method through the REAL Nextflow pipeline -- except "identity", the
# do-nothing baseline, which is scored directly against ground truth with no
# pipeline run at all (see Step 2 below).
#
# WHY THE REAL PIPELINE HERE and not the in-process unit driver
# (benchmarks/stare_bench/run_unit.py): the <=8 GB-per-process and fan-out
# claims only mean something under real resource limits and real task
# boundaries. Both paths call the same bin/tiled_*.py entry points, so
# neither can drift from the other.
#
# It deliberately does NOT invoke the old single-task registration entry
# point removed from bin/ when STARE became a four-stage fan-out. That script
# exists on no branch. Its last caller was the ANHIR/ACROBAT landmark harness,
# whose STARE leg had been broken since the split; that harness is now deleted.
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
#      rung is 10240^2 (105 Mpx, trimmed from 20480^2 before the first run --
#      see synthetic_gt.yaml's `size`): banded _warp keeps generation memory-safe,
#      and field.npy is skipped above DENSE_MAX_PIXELS (64 Mpx)
#      automatically -- ground truth is recovered parametrically from
#      truth.json's field_params_call. These pairs need real images.
#
# ADDED for the first real run (see the handoff of 2026-08-26):
#   4. The crop source is read from the config's `crop_source:` block instead
#      of being hardcoded to generated nuclei -- otherwise the HEADLINE result,
#      whose entire justification is real tissue, is measured on synthetic
#      texture. It is validated once, upfront, not per-pair.
#   5. An execution profile ($NF_PROFILE) is passed. This script previously
#      ran `-profile singularity` alone, so on a cluster every task went to
#      the LOCAL executor of the submitting node -- no SLURM, no ceiling.
#   6. The published per-tile *_ctrl.json and *_tre.json are discovered and
#      passed to the scorer, so the gate ROC -- the one novel metric here --
#      is measured on a real pipeline run rather than left None.
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

# Execution profile. Defaults to the IEO cluster under SLURM, because that is
# the only place this rung's scale is reachable: 132 pairs at 10240^2 is ~111 GB
# of float32 OME-TIFF before a single work directory, and no laptop in this
# project's history has had singularity on it.
#
#   singularity -- containers; the pipeline declares one per process.
#   slurm       -- process.executor = 'slurm', plus queue/account/qos from
#                  params.slurm_partition / slurm_account / slurm_qos (all
#                  null by default: a null queue submits to the partition
#                  SLURM itself defaults to).
#   ieo         -- pins max_cpus/max_memory/max_time to the IEO ceiling
#                  (128 / 700.GB / 240.h).
#
# ORDER MATTERS, and only benignly today. nextflow.config's `slurm` profile
# sets process.resourceLimits as a PLAIN MAP, evaluated eagerly at
# profile-merge time, which SHADOWS the top-level closure form -- so it freezes
# to whatever params.max_* were when `slurm` merged, and `ieo`'s later pins do
# NOT reach it. That is harmless here only because the top-level defaults and
# ieo's pins are the same three values; nextflow.config says so itself, at
# length, above the line. If either set ever changes, this benchmark starts
# submitting unclamped and the note below the `slurm` profile is the fix.
#
# Override for another site: NF_PROFILE=singularity,slurm ./run_mid.sh ...
NF_PROFILE="${NF_PROFILE:-singularity,slurm,ieo}"

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
from benchmarks.stare_bench.texture import describe, from_config

plan_csv, pairs_root, config_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

config = yaml.safe_load(config_path.read_text())
physics = config.get("physics", {})

# The crop source comes from the CONFIG, not from a literal here. This was
# previously hardcoded to the generated-nuclei source, which meant the headline
# numbers -- whose whole justification is that reviewers discount synthetic
# texture -- would have been measured on generated nuclei. from_config REFUSES
# an institutional block it cannot satisfy; it never degrades to synthetic.
crop_source = from_config(config)

# Validate ONCE, before the first pair, and fail the whole run here rather than
# on whichever row's seed happens to select the bad slide. The institutional
# source picks its slide FROM THE SEED, so an undersized or missing slide
# otherwise fails a scattered SUBSET of the plan, hundreds of rows deep.
crop_source.validate(tuple(config["size"]))
print(f"crop source: {json.dumps(describe(crop_source))}", flush=True)

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
            crop_source=crop_source,
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

# Locate the transform a completed run actually published, per backend.
# NEVER hardcoded to a guessed literal -- discovered from the SAME layout
# lib/Layout.groovy owns (patientDir(outdir, patient, kind) ->
# <outdir>/<patient>/<kind>) and conf/modules.config's publishDir rules.
#
#   tiled: TILED_SOLVE publishes the M0 + mesh manifest to
#     <outdir>/<patient>/registered/manifest/*_manifest.json (conf/modules.config's
#     withName: 'TILED_SOLVE' block).
#
#   ashlar: NOT FROM A PIPELINE RUN. ashlar stopped being a registration backend at
#     :fire: 6a54479, so there is no ASHLAR_SOLVE process and no published tree to
#     glob. benchmarks/ashlar/solve.py drives it directly and writes the SAME manifest
#     shape STARE emits -- which is why the glob below still covers both, and why the
#     single predict_from_manifest reads both. The ashlar leg of this script must
#     therefore point find_transform at that driver's output directory, NOT at an
#     <outdir> the pipeline never wrote. See external_baseline: in arms.yaml.
#
#   valis: REGISTER's registrar pickle IS published, as of the third
#     publishDir block in conf/modules.config's withName: 'REGISTER' -- it
#     lands at
#       <outdir>/<patient>/registered/transform/preprocessed/data/*_registrar.pickle
#     Note the preprocessed/data/ nesting: publishDir reproduces the process's
#     own output-directory structure under the declared path, exactly as
#     REGISTER's "summary" block does for *.csv. That is TWO levels deeper
#     than a naive registered/*.pickle glob, which is why the -path patterns
#     below lead with '*' rather than anchoring on registered/.
#
#     (An earlier revision of this comment said the pickle is never published
#     and that these globs were EXPECTED to find nothing. That was true when
#     it was written and stopped being true when the publishDir block landed.
#     The globs themselves were always broad enough -- verified against the
#     real published layout -- so only the prose was wrong. Left recorded
#     here because a comment asserting "this finds nothing" is what would
#     send the next reader to narrow a working glob.)
# The per-tile control-point JSONs and STARE's own *_tre.json, discovered the
# same way and for the same reason: named from conf/modules.config's publishDir
# rules, never guessed.
#
#   controls: TILED_REG_TILE publishes one *_ctrl.json PER TILE (its prefix
#     carries _${row.ix}_${row.iy}, so they do not collide) to
#     <outdir>/<patient>/registered/controls. These carry the accept/reject
#     data the GATE ROC is computed from -- the benchmark's one genuinely
#     novel metric, and unavailable to a transform-based score until this
#     publishDir landed.
#
#   tre: TILED_SOLVE publishes *_tre.json to <outdir>/<patient>/qc/registration.
#     STARE's self-reported rigid TRE -- circular by construction (built from
#     the same phase correlations the registration optimises), so it is
#     admitted to the table only under a diag_ prefix and can never appear as
#     accuracy.
#
# Both are STARE's alone. ASHLAR's manifest is deliberately written in STARE's
# format so one predictor reads both, but it has no *_ctrl.json and no STARE
# gate -- so these are looked up for method=tiled ONLY, and score_pair REFUSES
# controls passed with any other method rather than reporting STARE's
# confusion matrix under another backend's label.
find_controls() {
  local outdir="$1" pair_id="$2" method="$3" dir
  [[ "$method" != "tiled" ]] && { printf ''; return; }
  dir="$outdir/$pair_id/registered/controls"
  if compgen -G "$dir/*_ctrl.json" >/dev/null 2>&1; then printf '%s' "$dir"; fi
}

find_tre() {
  local outdir="$1" pair_id="$2" method="$3" hit
  [[ "$method" != "tiled" ]] && { printf ''; return; }
  hit="$(find "$outdir/$pair_id/qc/registration" -maxdepth 1 \
         -name '*_tre.json' 2>/dev/null | sort | head -n1)"
  printf '%s' "$hit"
}

find_transform() {
  local outdir="$1" pair_id="$2" method="$3" hit
  case "$method" in
    tiled|ashlar)
      hit="$(find "$outdir/$pair_id/registered/manifest" -maxdepth 1 \
             -name '*_manifest.json' 2>/dev/null | sort | head -n1)"
      ;;
    valis)
      hit="$(find "$outdir/$pair_id" \
             \( -path '*/preprocessed/data/*_registrar.pickle' \
                -o -path '*/registered/*_registrar.pickle' \) \
             2>/dev/null | sort | head -n1)"
      ;;
    *)
      hit=""
      ;;
  esac
  printf '%s' "$hit"
}

# --- Step 2: register + score each (pair, method) through the real pipeline -
# Pipeline defaults, not the unit rung's small ones: tile 2048, halo 256
# (benchmarks/stare_bench/cli.py's own argparse defaults already match).
while IFS=, read -r run_id pair_id method rest; do
  [[ "$run_id" == "run_id" ]] && continue
  outdir="$RESULTS_ROOT/$run_id"
  scored="$outdir/registration_synthetic_gt.csv"

  # Idempotent like generation: a crash partway through the plan must resume
  # from the next unscored row, not re-run every already-scored nextflow
  # invocation.
  if [[ -f "$scored" ]]; then
    echo "skip $run_id (already scored)"
    continue
  fi

  mkdir -p "$outdir"
  echo ">>> $run_id (pair=$pair_id method=$method)"

  # "identity" is the do-nothing baseline (zero displacement everywhere),
  # not a registration method -- there is no pipeline to run and no
  # transform to find. Score it directly against the pair's ground truth.
  if [[ "$method" == "identity" ]]; then
    python3 -m benchmarks.stare_bench.cli \
      --pair-dir "$PAIRS_ROOT/$pair_id" \
      --work-dir "$outdir/cli_work" \
      --run-id "$run_id" \
      --method identity \
      --tile 2048 \
      --halo 256 \
      --out "$scored"
    continue
  fi

  mkdir -p "$outdir/trace"

  # Pin the work directory PER RUN. All 396 invocations sharing the default
  # ./work would let conf/modules.config's errorStrategy 'ignore' branch (a
  # dropped task that never fails the run) plus the tiled fan-out's memory
  # profile at 10240^2 leave a degraded run's stale/partial task outputs
  # indistinguishable from a clean one's.
  nextflow -q run "$REPO_ROOT" \
    -profile "$NF_PROFILE" \
    -w "$outdir/work" \
    --input "$PAIRS_ROOT/$pair_id/samplesheet.csv" \
    --outdir "$outdir" \
    --registration_method "$method" \
    --reg_tiled_mode high \
    --start registration --stop registration \
    --reg_qc 2 \
    -with-trace "$outdir/trace/trace.txt"

  transform="$(find_transform "$outdir" "$pair_id" "$method")"
  if [[ -z "$transform" ]]; then
    echo "ERROR: no $method transform found under $outdir/$pair_id -- refusing to score $run_id (a missing transform must never fall back to a STARE re-run)" >&2
    exit 1
  fi

  # Gate ROC and the diag_ intrinsic TRE, when the run published them. Both are
  # OPTIONAL by design: omitting them leaves those columns None (unmeasured),
  # never 0.0/0.5 computed from an empty set. So a backend or a run that has
  # neither degrades to fewer columns, not to fabricated ones.
  score_args=()
  controls="$(find_controls "$outdir" "$pair_id" "$method")"
  [[ -n "$controls" ]] && score_args+=(--controls "$controls")
  tre="$(find_tre "$outdir" "$pair_id" "$method")"
  [[ -n "$tre" ]] && score_args+=(--intrinsic-tre "$tre")

  # --tile/--halo are NOT free parameters here: --halo is the max_disp half of
  # bin/tiled_solve.py's control-point gate, and score_pair reconstructs that
  # gate to build the ROC. It must equal what the run used -- 256, from
  # reg_tiled_mode=high above, since params.reg_tiled_max_disp is null by
  # default and falls back to the halo. Registering at one mode and scoring at
  # another halo would silently measure a decision the pipeline never made, and
  # nothing downstream can catch it: the manifest records neither threshold.
  python3 -m benchmarks.stare_bench.cli \
    --pair-dir "$PAIRS_ROOT/$pair_id" \
    --work-dir "$outdir/cli_work" \
    --run-id "$run_id" \
    --method "$method" \
    --transform "$transform" \
    --tile 2048 \
    --halo 256 \
    "${score_args[@]+"${score_args[@]}"}" \
    --out "$scored"
done < "$PLAN"
