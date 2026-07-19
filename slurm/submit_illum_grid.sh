#!/usr/bin/env bash
#
# Submit the full illumination-correction variant grid as a SLURM job array —
# ONE variant per array task, running in parallel — then a dependent
# aggregation job that builds the combined report.html once the array finishes.
#
# Each array task runs:  illum_benchmark.py --variant <NAME> ... (its own
# pyramid + plots + parts/<NAME>.json). The aggregate task runs:
# illum_benchmark.py --aggregate ... (metrics.json + report.html from all parts).
#
# Usage:
#   1. Edit the CONFIG block below.
#   2. bash slurm/submit_illum_grid.sh
#
set -euo pipefail

# ----------------------------- CONFIG (edit) -----------------------------
REPO="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
IMAGE="/scratch/$USER/stitched_mosaic.ome.tiff"     # your stitched mosaic
OUTDIR="/scratch/$USER/illum_bench"                 # scratch dir, 100 GB+ free
CHANNELS="DAPI CD3 CD8 CD4"                          # real names, in channel order
APPROX_TILE=1950                                     # true FOV pitch in px

# How to activate a python env with: numpy scipy tifffile matplotlib
# (optional: scikit-image for rolling_ball, basicpy for the baseline-basic anchor)
ENV_ACTIVATE="source /scratch/$USER/illumenv/bin/activate"

# Per-variant array-task resources — tune MEM to your mosaic (pyramid writing
# holds the full multichannel array in RAM: ~n_channels * H * W * 2 bytes).
CPUS=4
MEM=64G
TIME=02:00:00
MAX_CONCURRENT=8            # array throttle: at most this many variants at once
PARTITION=""               # e.g. PARTITION="--partition=cpu"; leave "" for default
# -------------------------------------------------------------------------

BENCH="$REPO/bin/illum_benchmark.py"
mkdir -p "$OUTDIR"/{parts,plots,pyramids,logs}

# 1) Enumerate the full grid (no image needed) -> one variant name per line.
eval "$ENV_ACTIVATE"
python "$BENCH" --list-variants --full-grid --outdir "$OUTDIR" > "$OUTDIR/variants.txt"
N=$(wc -l < "$OUTDIR/variants.txt")
echo "Full grid: $N variants -> $OUTDIR/variants.txt"
[ "$N" -ge 1 ] || { echo "no variants enumerated; aborting"; exit 1; }

# 2) Array job: one variant per task, in parallel (throttled to %MAX_CONCURRENT).
#    SLURM_ARRAY_TASK_ID selects the matching line from variants.txt.
ARRAY_ID=$(sbatch --parsable \
  --job-name=illum-grid \
  --array="1-${N}%${MAX_CONCURRENT}" \
  --cpus-per-task="$CPUS" --mem="$MEM" --time="$TIME" $PARTITION \
  --output="$OUTDIR/logs/variant-%a.out" \
  --wrap="set -euo pipefail; ${ENV_ACTIVATE}; \
    NAME=\$(sed -n \"\${SLURM_ARRAY_TASK_ID}p\" '$OUTDIR/variants.txt'); \
    echo \"=== variant: \$NAME ===\"; \
    python '$BENCH' --variant \"\$NAME\" --full-grid \
      --image '$IMAGE' --outdir '$OUTDIR' --channels $CHANNELS --approx-tile $APPROX_TILE")
echo "Submitted array job $ARRAY_ID  (1-$N%$MAX_CONCURRENT)"

# 3) Aggregation job: runs AFTER the whole array finishes, regardless of any
#    individual variant failure (afterany, not afterok), and builds the report
#    from whatever parts/ exist — so one OOM'd variant doesn't block the report.
AGG_ID=$(sbatch --parsable \
  --job-name=illum-aggregate \
  --dependency="afterany:${ARRAY_ID}" \
  --cpus-per-task=1 --mem=8G --time=00:20:00 $PARTITION \
  --output="$OUTDIR/logs/aggregate.out" \
  --wrap="set -euo pipefail; ${ENV_ACTIVATE}; python '$BENCH' --aggregate --outdir '$OUTDIR'")
echo "Submitted aggregate job $AGG_ID  (afterany:$ARRAY_ID)"

cat <<EOF

Queued. Track with:  squeue -u \$USER
When finished, on a machine with a display:
  - open $OUTDIR/report.html
  - load $OUTDIR/pyramids/*.ome.tiff into a QuPath project
  - first check $OUTDIR/plots/grid.png (grid overlay must sit on real FOV seams)
Per-variant stdout is under $OUTDIR/logs/variant-<arrayid>.out
EOF
