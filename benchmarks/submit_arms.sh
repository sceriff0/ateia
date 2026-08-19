#!/usr/bin/env bash
#SBATCH --job-name=mirage_arms
#SBATCH --output=logs/arms_%j.out
#SBATCH --error=logs/arms_%j.err
#SBATCH --time=168:00:00
#SBATCH --cpus-per-task=4    # headroom for CONCURRENCY Nextflow heads (they poll SLURM, not compute)
#SBATCH --mem=32G            # ALL heads share this; NXF_OPTS -Xmx caps each head's heap (below)
#SBATCH --partition=normal
#
# ============================================================================
# MIRAGE real-sample ARM benchmark — SLURM "head" (orchestrator) job
# ============================================================================
# The sibling of submit_sweep.sh, for the REAL slides rather than the synthetic
# matrix. See docs/benchmarks_real.md for what the arms are and why.
#
# This job is LIGHTWEIGHT. It runs Nextflow orchestrators, which submit ONE SLURM
# job per pipeline process (executor='slurm' in conf/ieo.config). The heavy compute
# — PREPROCESS, REGISTER, SEGMENT, QUANTIFY — runs in those CHILD jobs, sized by
# conf/base.config + modules.config, NOT here. Keep this job small; give it a LONG
# walltime, because it lives for the whole benchmark.
#
# THE CODE may live in $HOME (it is small). THE DATA MUST NOT. BENCH_DIR below is
# the beegfs storage root and holds the run plan, every arm's --outdir and every
# Nextflow work dir -- run_arms.sh puts each run's work under
# <RESULTS>/.launch/<run_id>/work, so nothing large is ever written to $HOME,
# which is small and read-only inside the containers.
#
# The only thing written back into the checkout is benchmarks/paper_data/*.csv in
# step 3 (a few hundred KB of tables); pull_to_ihc_method.sh reads them from there.
#
# Prereq (login node, once): the benchmark lives on the `benchmarking` branch, so
# the checkout must be on it --
#   git -C ~/pipelines/mirage fetch origin
#   git -C ~/pipelines/mirage checkout benchmarking
#   git -C ~/pipelines/mirage pull
#
# Submit:  cd /beegfs/scratch/ieo7660/analysis_runs/method_paper/benchmark
#          mkdir -p logs && sbatch ~/pipelines/mirage/benchmarks/submit_arms.sh
# Watch:   squeue -u $USER                 # 1 head job + N child jobs
#          tail -f logs/arms_<jobid>.out
# ============================================================================

# ---- EDIT THESE FOR YOUR SITE --------------------------------------------------
BENCH_DIR="/beegfs/scratch/ieo7660/analysis_runs/method_paper/benchmark"
SRC_DIR="$HOME/pipelines/mirage"          # the checkout. NOTE: unquoted $HOME, never "~/..."
INPUT="/beegfs/scratch/ieo7660/analysis_runs/method_paper/new_samples/input.csv"
RESULTS="$BENCH_DIR/arm_results"          # every arm's outdir + work dir lands under here
ARMS_YAML="$SRC_DIR/benchmarks/configs/arms.yaml"
PROFILES="singularity,ieo"                # OVERRIDES run_arms.sh's default -profile docker
SITE_CONFIG="$SRC_DIR/conf/ieo.config"    # gitignored: executor=slurm + cacheDir + paths
CONDA_ENV="nf-env"
CONCURRENCY="${ARMS_CONCURRENCY:-4}"      # arms launched AT ONCE. Each is one Nextflow head.
                                          # 4 heads x 3 GB heap = 12 GB < --mem=32G. RAISE --mem
                                          # BEFORE raising this: N heads x -Xmx must fit, and the
                                          # default NXF_JVM_ARGS people copy from the normal
                                          # launcher (-Xmx32g) would blow a 32 GB job at N=2.
ENABLE_CSE="${ENABLE_CSE:-false}"         # true => score the segmentation arms with CSE.
                                          # Needs bolt3x/attend_image_analysis:segeval published.
# -------------------------------------------------------------------------------

# NOTE: do NOT write SRC_DIR="~/..." — bash does tilde expansion BEFORE parameter
# expansion, so a tilde arriving via a variable stays a literal "~" and the path
# never resolves. Use an absolute path or "$HOME/...".

cd "$BENCH_DIR"
mkdir -p logs

# shellcheck disable=SC1090
source ~/.bashrc
conda activate "$CONDA_ENV"
command -v nextflow >/dev/null || { echo "nextflow not on PATH (check CONDA_ENV)"; exit 1; }
# Resolve the interpreter rather than assuming `python` exists. A conda env can
# provide nextflow while exposing python only as `python3` (or leaving python in
# `base`), and with no `set -e` here a bare `python ...` would fail, the plan
# would not be written, and the launch would proceed against a stale one. Observed
# on this cluster: "line 112: python: command not found".
PYTHON="${PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$PYTHON" ] || { echo "no python3/python on PATH (check CONDA_ENV)"; exit 1; }
echo "Using python: $PYTHON ($("$PYTHON" --version 2>&1))"
# The plan builder needs PyYAML. If PYTHON resolved to a system interpreter rather
# than the conda env's, the import fails INSIDE the plan step, which without
# `set -e` just leaves the plan unwritten. Check it here so the message names the
# real problem.
"$PYTHON" -c "import yaml" 2>/dev/null || {
    echo "ERROR: $PYTHON cannot import yaml (PyYAML)." >&2
    echo "       conda activate $CONDA_ENV && conda install pyyaml   (or pip install pyyaml)" >&2
    echo "       or set PYTHON=/path/to/the/right/python before sbatch." >&2
    exit 1
}

# Pin Nextflow: NF 26.x dropped the automatic lib/*.groovy class loading this
# pipeline relies on (ParamUtils/CsvUtils) and rejects the CLI boolean forms below.
export NXF_VER="${NXF_VER:-25.04.7}"
# Singularity image cache off read-only $HOME (matches conf/ieo.config's cacheDir).
export SINGULARITY_CACHEDIR="${SINGULARITY_CACHEDIR:-/hpcnfs/scratch/P_DIMA_ATTEND/users/vfassi/docker_images}"
export NXF_SINGULARITY_CACHEDIR="${NXF_SINGULARITY_CACHEDIR:-$SINGULARITY_CACHEDIR}"
# Cap EACH concurrent head's heap so CONCURRENCY x heap stays under --mem.
# This is NOT the -Xmx32g of a single-run launcher: that sizes ONE head, and here
# there are CONCURRENCY of them sharing one allocation.
export NXF_OPTS="${NXF_OPTS:--Xms512m -Xmx3g}"

# Concurrency is passed on the COMMAND LINE, not via benchmark.config. Every
# per-process cap in conf/modules.config is Math.min(own, params.max_forks), evaluated
# EAGERLY when that file is parsed -- which happens before a -c file is merged, so a -c
# cannot reach the clamps. The two must be raised TOGETHER: the lower of the pair binds,
# and at the shipped defaults queue_size (20) is far below max_forks (100), so raising
# max_forks alone does nothing. See docs/resources.md.
MAX_FORKS="${MAX_FORKS:-5}"
QUEUE_SIZE="${QUEUE_SIZE:-20}"

EXTRA_ARGS=(--max_forks "$MAX_FORKS" --queue_size "$QUEUE_SIZE")
# CSE is enabled by a PROFILE, never `--skip_seg_quality_eval false`: Nextflow 26
# delivers every --param as a String, so that flag sets the param to the truthy
# string "false" and disables the scorer while reading like it enables it.
if [[ "$ENABLE_CSE" == "true" ]]; then
    PROFILES="$PROFILES,seg_quality"
fi

# CellSAM weights are gated. singularity.envWhitelist forwards DEEPCELL_ACCESS_TOKEN
# by REFERENCE, so it must exist in the environment on the COMPUTE node — a site
# launching with --export=NONE silently stops delivering it. Warn now rather than
# let a multi-hour segmentation arm die on the download. Set cellsam_model_path in
# your site config instead if the compute nodes have no internet.
if grep -q "cellsam" "$ARMS_YAML" && [[ -z "${DEEPCELL_ACCESS_TOKEN:-}" ]]; then
    echo "WARNING: arms.yaml uses the cellsam backend but DEEPCELL_ACCESS_TOKEN is empty."
    echo "         Get a token at https://users.deepcell.org, then either"
    echo "           export DEEPCELL_ACCESS_TOKEN=... in ~/.bashrc (sourced above), or"
    echo "           set params.cellsam_model_path to pre-downloaded weights."
fi

echo "=================================================="
echo "Head job ${SLURM_JOB_ID:-local} on ${SLURM_NODELIST:-$(hostname)}"
echo "Start:      $(date)"
echo "Bench dir:  $BENCH_DIR"
echo "Input:      $INPUT"
echo "Results:    $RESULTS"
echo "Profiles:   $PROFILES   Concurrency: $CONCURRENCY   CSE: $ENABLE_CSE"
echo "=================================================="

# 1. Expand arms.yaml -> arm_plan.csv + the consumer's arms.csv (seconds, local).
#    --results-root puts arms.csv where registration_arms.R looks for it.
"$PYTHON" "$SRC_DIR/benchmarks/build_arm_plan.py" \
    --arms         "$ARMS_YAML" \
    --input        "$INPUT" \
    --out          "$BENCH_DIR/arm_plan.csv" \
    --results-root "$RESULTS"

# Checked explicitly because there is no `set -e` here: without this, a failed or
# skipped plan step falls straight through to run_arms.sh, which would launch days
# of cluster work against a STALE plan from a previous submission.
if [ ! -s "$BENCH_DIR/arm_plan.csv" ]; then
    echo "ERROR: $BENCH_DIR/arm_plan.csv was not written; not launching." >&2
    exit 1
fi

# 2. Launch. run_arms.sh runs the passes in order — preprocess, registration,
#    segmentation, compute — with a barrier between: each pass resumes from a
#    checkpoint the previous one wrote, and the compute arm is timed last and
#    alone so its numbers are not taken under self-inflicted contention.
#    Pass the profile via ARMS_PROFILE, NOT a trailing -profile: Nextflow accepts
#    -profile only once. A trailing -c IS fine (Nextflow merges multiple -c).
export ARMS_CONCURRENCY="$CONCURRENCY"
export ARMS_PROFILE="$PROFILES"
"$SRC_DIR/benchmarks/run_arms.sh" \
    "$BENCH_DIR/arm_plan.csv" \
    "$INPUT" \
    "$RESULTS" \
    -c "$SITE_CONFIG" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

echo "=================================================="
echo "Arms finished: $(date)"
echo
echo "Next, on a login node — emit the tables:"
echo "    cd $SRC_DIR"
echo "    python -m benchmarks.analysis.make_tables \\"
echo "        --results-root $RESULTS --run-plan $BENCH_DIR/arm_plan.csv \\"
echo "        --outdir benchmarks/paper_data"
echo "    python -m benchmarks.analysis.make_figures \\"
echo "        --results-root $RESULTS --run-plan $BENCH_DIR/arm_plan.csv \\"
echo "        --outdir benchmarks/analysis"
echo
echo "Then hand off to ihc_method (small QC artifacts only — the images stay here):"
echo "    benchmarks/pull_to_ihc_method.sh $RESULTS <path-to>/ihc_method"
echo "=================================================="
