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
# EVERYTHING LIVES ON BEEGFS. $HOME is small and read-only inside the containers;
# the work dirs and published outputs of a real-WSI benchmark are large. BENCH_DIR
# below is the beegfs storage root, and the checkout, the plan, the work dirs and
# the results all sit under it.
#
# Prereq (login node, once):
#   cd /beegfs/scratch/$USER/analysis_runs/method_paper/benchmark
#   git clone -b benchmarking <repo> mirage       # if not already there
#   # write real_input.csv: patient_id,path_to_file,is_reference,channels
#
# Submit:  cd $BENCH_DIR && mkdir -p logs && sbatch mirage/benchmarks/submit_arms.sh
# Watch:   squeue -u $USER                 # 1 head job + N child jobs
#          tail -f logs/arms_<jobid>.out
# ============================================================================
set -euo pipefail

# ---- EDIT THESE FOR YOUR SITE --------------------------------------------------
BENCH_DIR="/beegfs/scratch/ieo7660/analysis_runs/method_paper/benchmark"
SRC_DIR="$BENCH_DIR/mirage"               # the checkout (NOT the one in $HOME)
INPUT="$BENCH_DIR/real_input.csv"         # your real slides
RESULTS="$BENCH_DIR/arm_results"          # every arm's outdir lands under here
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

# ~/.bashrc, /etc/bashrc and `conda activate` reference unset vars that trip `set -u`.
# Relax nounset for the environment init only, then restore it.
set +u
# shellcheck disable=SC1090
source ~/.bashrc
conda activate "$CONDA_ENV"
set -u
command -v nextflow >/dev/null || { echo "nextflow not on PATH (check CONDA_ENV)"; exit 1; }

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

EXTRA_ARGS=()
if [[ "$ENABLE_CSE" == "true" ]]; then
    EXTRA_ARGS+=(--skip_seg_quality_eval false)
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
python "$SRC_DIR/benchmarks/build_arm_plan.py" \
    --arms         "$ARMS_YAML" \
    --input        "$INPUT" \
    --out          "$BENCH_DIR/arm_plan.csv" \
    --results-root "$RESULTS"

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
