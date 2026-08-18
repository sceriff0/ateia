#!/usr/bin/env bash
# Pull mirage benchmark artifacts into ihc_method/data/ — the analysis hand-off.
#
# Usage:
#   benchmarks/pull_to_ihc_method.sh <arm_results_root> <ihc_method_dir>
#
# Local (both repos side by side under Github/):
#   benchmarks/pull_to_ihc_method.sh arm_results ../ihc_method
#
# From a laptop, pulling off the cluster (note the trailing context: rsync runs
# over ssh only for the SOURCE, so run this ON the cluster and rsync the small
# result out, or mount the cluster path and point at it directly):
#   benchmarks/pull_to_ihc_method.sh /hpcnfs/.../arm_results ~/Github/ihc_method
#
# WHY A SCRIPT AND NOT A COPY. Three different mirage output sets land in three
# different places in ihc_method, and only ONE of them is the whole outdir:
#
#   <arm_root>/<arm>/<patient>/qc|registered   -> data/registration_arms/<arm>/...
#   <arm_root>/arms.csv                        -> data/registration_arms/arms.csv
#   benchmarks/paper_data/*.csv                -> data/benchmark/
#   benchmarks/analysis/measurements.csv etc.  -> data/benchmark/
#   one representative run's outdir            -> data/mirage/<patient>/
#
# ONLY THE SMALL ARTIFACTS ARE COPIED. The registered OME-TIFFs and masks are
# multi-GB per patient and no ihc_method page reads them — every figure reads
# JSON/CSV QC. Copying the images would move terabytes to answer nothing, so the
# include filters below are an allowlist, not an optimisation.
set -euo pipefail

ARM_ROOT="${1:?arm results root - the results_root given to run_arms.sh}"
IHC="${2:?path to the ihc_method repo}"

[[ -d "$ARM_ROOT" ]] || { echo "no such directory: $ARM_ROOT" >&2; exit 1; }
[[ -d "$IHC" ]]      || { echo "no such directory: $IHC" >&2; exit 1; }
ARM_ROOT="$(cd "$ARM_ROOT" && pwd)"
IHC="$(cd "$IHC" && pwd)"
PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ -f "$IHC/_workflowr.yml" ]] || {
  echo "WARNING: $IHC has no _workflowr.yml — is that really the ihc_method repo?" >&2; }

DEST_ARMS="$IHC/data/registration_arms"
DEST_BENCH="$IHC/data/benchmark"
mkdir -p "$DEST_ARMS" "$DEST_BENCH"

echo "=== 1/4  registration arms -> data/registration_arms/ ==="
# --prune-empty-dirs keeps the tree to arms that actually produced QC: an arm
# directory with no seg_qc.json is an arm that failed, and arm_manifest() counts
# any directory holding QC as an arm. Copying empty ones would put an empty box
# in every panel rather than an honest absence.
rsync -a --prune-empty-dirs \
  --include='*/' \
  --include='qc/**' \
  --include='registered/summary/**' \
  --include='csv/*.csv' \
  --include='trace/trace.txt' \
  --exclude='*' \
  "$ARM_ROOT"/ "$DEST_ARMS"/

if [[ -f "$ARM_ROOT/arms.csv" ]]; then
  cp "$ARM_ROOT/arms.csv" "$DEST_ARMS/arms.csv"
  echo "  arms.csv copied (labels explicit — directory-name parsing bypassed)"
else
  # Not fatal, but say so loudly. Without it registration_arms.R parses the
  # directory names, and a mislabelled arm renders cleanly with the conclusion
  # inverted rather than failing.
  echo "  WARNING: no arms.csv at $ARM_ROOT — the consumer will fall back to" >&2
  echo "           parsing directory names. Re-run build_arm_plan.py with" >&2
  echo "           --results-root $ARM_ROOT to generate it." >&2
fi

echo "=== 2/4  paper DATA tables -> data/benchmark/ ==="
copied=0
for f in "$PIPELINE_DIR"/benchmarks/paper_data/*.csv; do
  [[ -e "$f" ]] || continue
  cp "$f" "$DEST_BENCH"/ && copied=$((copied + 1))
done
[[ "$copied" -gt 0 ]] \
  && echo "  $copied table(s) from benchmarks/paper_data/" \
  || echo "  none yet — run: python -m benchmarks.analysis.make_tables ..."

echo "=== 3/4  measurements + resource stats -> data/benchmark/ ==="
copied=0
for f in measurements.csv resource_models.csv resource_stats.csv run_cost.csv; do
  src="$PIPELINE_DIR/benchmarks/analysis/$f"
  [[ -f "$src" ]] && { cp "$src" "$DEST_BENCH"/; copied=$((copied + 1)); }
done
[[ "$copied" -gt 0 ]] \
  && echo "  $copied file(s) from benchmarks/analysis/" \
  || echo "  none yet — run: python -m benchmarks.analysis.make_figures ..."

echo "=== 4/4  one full run -> data/mirage/ (registration_run_qc.Rmd) ==="
# registration_run_qc.Rmd answers "was THIS cohort registered well enough to
# analyse?" and reads a single run's outdir, keyed per patient. The compute arm
# is the right source: it is the only arm that ran the FULL pipeline, so it is
# the only one whose tree carries postprocessing QC too.
SRC_RUN=""
for cand in "$ARM_ROOT"/compute_* "$ARM_ROOT"/valis_high_micro2; do
  [[ -d "$cand" ]] && { SRC_RUN="$cand"; break; }
done
if [[ -n "$SRC_RUN" ]]; then
  mkdir -p "$IHC/data/mirage"
  rsync -a --prune-empty-dirs \
    --include='*/' --include='qc/**' --include='csv/*.csv' --exclude='*' \
    "$SRC_RUN"/ "$IHC/data/mirage"/
  echo "  from $(basename "$SRC_RUN")"
else
  echo "  no compute_* or valis_high_micro2 arm found — skipped"
fi

echo
echo "Done. In $IHC:"
echo "  Rscript -e 'workflowr::wflow_build(c(\"analysis/registration_arms.Rmd\", \\"
echo "                                       \"analysis/benchmark_pipeline.Rmd\", \\"
echo "                                       \"analysis/benchmark_registration.Rmd\", \\"
echo "                                       \"analysis/registration_run_qc.Rmd\"))'"
echo
echo "data/ is gitignored in ihc_method — nothing here is committed."
