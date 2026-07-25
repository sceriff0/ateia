#!/usr/bin/env bash
#SBATCH --job-name=mirage_bench
#SBATCH --output=logs/bench_%j.out
#SBATCH --error=logs/bench_%j.err
#SBATCH --time=72:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
# #SBATCH --partition=<your_partition>     # uncomment + set if your site needs it
# #SBATCH --mail-type=END,FAIL
# #SBATCH --mail-user=you@ieo.it
#
# ============================================================================
# MIRAGE benchmark sweep — SLURM "head" (orchestrator) job
# ============================================================================
# This job is LIGHTWEIGHT: it runs the Nextflow orchestrator, which submits ONE
# SLURM job per pipeline process (executor='slurm' in conf/ieo.config). The heavy
# compute — CONVERT_IMAGE, PREPROCESS, REGISTER / REG_*, SEGMENT, QUANTIFY — runs
# in those CHILD jobs, sized by conf/base.config + modules.config, NOT here. So keep
# this job small (2 cpus / 16 GB for the Nextflow JVM) but give it a LONG walltime:
# run_sweep.sh launches the runs sequentially, so the head job lives for the whole sweep.
#
# Prereqs (run the two python steps yourself first, on a login node):
#   python benchmarks/generate_matrix.py --source <img>.ome.tif --outdir bench_matrix \
#       --sweep benchmarks/configs/sweep.yaml
#   python benchmarks/build_run_plan.py  --sweep benchmarks/configs/sweep.yaml \
#       --out bench_run_plan.csv --repeats 3
#
# Submit:  mkdir -p logs && sbatch benchmarks/submit_sweep.sh
# Watch:   squeue -u $USER        # 1 head job + N child jobs
#          tail -f logs/bench_<jobid>.out
# ============================================================================
set -euo pipefail

# ---- EDIT THESE FOR YOUR SITE --------------------------------------------------
SRC_DIR="/beegfs/scratch/ieo7660/analysis_runs/method_paper/benchmark/mirage"
MATRIX_DIR="bench_matrix"                 # from generate_matrix --outdir
RUN_PLAN="bench_run_plan.csv"             # from build_run_plan --out
RESULTS="bench_results"                   # per-run outputs land here
PROFILES="singularity,ieo"               # OVERRIDES run_sweep.sh's default -profile docker
SITE_CONFIG="conf/ieo.config"            # gitignored: executor=slurm + singularity cacheDir + paths
CONDA_ENV="nf-env"                        # env that has nextflow + python
CONCURRENCY="${SWEEP_CONCURRENCY:-6}"    # pipeline runs launched AT ONCE (each = 1 Nextflow head that
                                          # submits its OWN SLURM process jobs). 6 heads fit in --mem=32G;
                                          # raise for more sweep parallelism (and bump --mem: ~2-3 GB/head).
# -------------------------------------------------------------------------------

cd "$SRC_DIR"
mkdir -p logs

# ~/.bashrc + /etc/bashrc + `conda activate` reference unset vars (e.g. BASHRCSOURCED) that trip
# `set -u`. Relax nounset just for the environment init, then restore it.
set +u
# shellcheck disable=SC1090
source ~/.bashrc
conda activate "$CONDA_ENV"
set -u
command -v nextflow >/dev/null || { echo "nextflow not on PATH (check CONDA_ENV / module load)"; exit 1; }
# Keep Singularity image cache off read-only $HOME (matches conf/ieo.config's cacheDir).
export NXF_SINGULARITY_CACHEDIR="${NXF_SINGULARITY_CACHEDIR:-/hpcnfs/scratch/P_DIMA_ATTEND/users/vfassi/docker_images}"

echo "=================================================="
echo "Head job ${SLURM_JOB_ID:-local} on ${SLURM_NODELIST:-$(hostname)}"
echo "Start: $(date)   Profiles: $PROFILES   Results: $RESULTS"
echo "=================================================="

# run_sweep.sh: one `nextflow run` per run_plan row; each submits its process jobs to SLURM.
# SWEEP_CONCURRENCY launches several of those runs at once (parallelising the sweep across the cluster).
# SWEEP_PROFILE sets the (single) Nextflow -profile; the site config is added as a trailing -c.
# NB: pass the profile via SWEEP_PROFILE, NOT a trailing -profile — Nextflow allows -profile only once.
export SWEEP_CONCURRENCY="$CONCURRENCY"
export SWEEP_PROFILE="$PROFILES"
benchmarks/run_sweep.sh \
    "$RUN_PLAN" \
    "$MATRIX_DIR/matrix_manifest.csv" \
    "$RESULTS" \
    -c "$SITE_CONFIG"

echo "=================================================="
echo "Sweep finished: $(date)"
echo "Next (login node) — emit the paper DATA tables:"
echo "    python -m benchmarks.analysis.make_tables \\"
echo "        --results-root $RESULTS --run-plan $RUN_PLAN \\"
echo "        --manifest $MATRIX_DIR/matrix_manifest.csv --outdir benchmarks/paper_data"
echo "Optional figures + derived config:"
echo "    python -m benchmarks.analysis.make_figures \\"
echo "        --results-root $RESULTS --run-plan $RUN_PLAN \\"
echo "        --manifest $MATRIX_DIR/matrix_manifest.csv --outdir benchmarks/analysis"
echo "=================================================="
