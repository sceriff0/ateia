#!/usr/bin/env bash
# Drive the REAL-SAMPLE arm benchmark: one pipeline launch per arm_plan.csv row.
#
# Usage:
#   benchmarks/run_arms.sh <arm_plan.csv> <real_input.csv> <results_root> [extra nextflow args...]
#
# Example (cluster):
#   ARMS_PROFILE="singularity,ieo" ARMS_CONCURRENCY=3 \
#     benchmarks/run_arms.sh arm_plan.csv real_input.csv arm_results
#
# The sibling of run_sweep.sh, and deliberately simpler in one way: the
# samplesheet is GIVEN (your real slides) rather than synthesized from a matrix,
# so there is no cell resolution and no channel-name construction here.
#
# WHAT IT WRITES — exactly the tree ihc_method/code/registration_arms.R reads:
#
#     <results_root>/<arm>/<patient>/qc/registration/*_seg_qc.json
#     <results_root>/<arm>/<patient>/registered/summary/*.csv     (VALIS arms)
#     <results_root>/arms.csv                                     (build_arm_plan.py)
#
# That is just `--outdir <results_root>/<arm>`; Layout.patientDir() is already
# `<outdir>/<patient_id>/<kind>`. Nothing is repackaged, so the consumer reads
# run_qc.R's readers unchanged.

PLAN="${1:?arm_plan.csv}"
INPUT="${2:?real input.csv}"
ROOT="${3:?results root}"
shift 3
EXTRA=("$@")

mkdir -p "$ROOT"
ROOT="$(cd "$ROOT" && pwd)"
START_DIR="$PWD"
PIPELINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$INPUT" = /* ]] || INPUT="$START_DIR/$INPUT"

# Pin Nextflow to the supported 25.04 line, as run_sweep.sh does. NF 26.x dropped
# the automatic lib/*.groovy class loading this pipeline relies on
# (ParamUtils/CsvUtils) and rejects the CLI boolean forms used below.
export NXF_VER="${NXF_VER:-25.04.7}"

PROFILE="${ARMS_PROFILE:-docker}"
CONCURRENCY="${ARMS_CONCURRENCY:-1}"

# benchmark.config turns on enable_trace + enable_size_logs, which is what makes
# every arm contribute cost rows to measurements.csv alongside its QC. It costs
# nothing on the QC arms and is the whole point of the compute arm.
BENCH_CONF="$PIPELINE_DIR/benchmarks/configs/benchmark.config"

# Absolutize any -c/-config/-params-file in the pass-through args: each run is
# launched from its own directory (isolated .nextflow/ so parallel Nextflow heads
# do not fight over .nextflow/history on NFS/beegfs), which breaks relative paths.
EXTRA_ABS=(); _i=0
while (( _i < ${#EXTRA[@]} )); do
  _a="${EXTRA[$_i]}"; EXTRA_ABS+=("$_a")
  case "$_a" in
    -c|-C|-config|-params-file)
      _i=$((_i + 1)); _v="${EXTRA[$_i]:-}"
      [[ -n "$_v" && "$_v" != /* && -e "$_v" ]] && _v="$(cd "$(dirname "$_v")" && pwd)/$(basename "$_v")"
      EXTRA_ABS+=("$_v") ;;
  esac
  _i=$((_i + 1))
done

header=$(head -n1 "$PLAN" | tr -d '\r')
IFS=',' read -r -a cols <<< "$header"

col_index() {
  local name="$1" i
  for i in "${!cols[@]}"; do
    [[ "${cols[$i]}" == "$name" ]] && echo "$i" && return
  done
  echo -1
}

# col_val <name> <vals...>. build_arm_plan.py guarantees no value contains a comma
# (labels live only in arms.csv, which R reads), so this naive IFS split is safe —
# the same contract run_sweep.sh relies on. benchmarks/tests/test_build_arm_plan.py
# asserts it, because a comma sneaking into a value would shift every later column
# on that row and silently launch the wrong configuration.
col_val() {
  local name="$1"; shift
  local idx; idx=$(col_index "$name")
  [[ "$idx" -lt 0 ]] && { echo ""; return; }
  local vals=("$@")
  echo "${vals[$idx]:-}"
}

# add_param <flag> <value>: append `--flag value` ONLY when value is non-empty.
#
# The blank check is load-bearing, not defensive. A tiled/STARE arm has NO
# memory_mode and NO reg_micro_reg — those are VALIS-only params — so those cells
# are legitimately blank. Passing `--memory_mode ""` would set the param to an
# empty string rather than leave it at its default, and schema validation would
# reject it (or worse, accept it and register at a configuration nobody chose).
ARGS=()
add_param() { [[ -n "${2:-}" ]] && ARGS+=("--$1" "$2") || true; }

launch() {                       # launch <run_id> <arm> <input> <outdir> <args...>
  local run_id="$1" arm="$2" in_csv="$3" outdir="$4"; shift 4
  local run_args=("$@")
  local rundir="$ROOT/.launch/$run_id"
  mkdir -p "$rundir" "$outdir" "$outdir/trace"
  echo "[$run_id] arm=$arm -> $outdir"
  (
    cd "$rundir"
    nextflow -q run "$PIPELINE_DIR" \
      -profile "$PROFILE" \
      -c "$BENCH_CONF" \
      -work-dir "$rundir/work" \
      -name "arms-$run_id" \
      --input "$in_csv" \
      --outdir "$outdir" \
      --trace_dir "$outdir/trace" \
      "${run_args[@]}" \
      "${EXTRA_ABS[@]+"${EXTRA_ABS[@]}"}" \
      > "$outdir/nextflow.stdout.log" 2> "$outdir/nextflow.stderr.log" \
      || {
        # Nextflow writes most run-level errors (validation, missing input, a failed
        # task's .command.err excerpt) to STDOUT, not stderr -- a message naming only
        # the stderr log sends you to a file containing just the version banner.
        # Show both, and name the .nextflow.log, which has the rest.
        echo "[$run_id] FAILED" >&2
        echo "----- last 25 lines of stdout ($outdir/nextflow.stdout.log) -----" >&2
        tail -n 25 "$outdir/nextflow.stdout.log" >&2 2>/dev/null
        echo "----- last 15 lines of stderr ($outdir/nextflow.stderr.log) -----" >&2
        tail -n 15 "$outdir/nextflow.stderr.log" >&2 2>/dev/null
        echo "----- full log: $rundir/.nextflow.log -----" >&2
      }
  )
}

# filtered_sheet <patient> <dest>: the real samplesheet restricted to one patient.
# There is no --only_patient param; restricting the cohort means writing a smaller
# samplesheet. Header + every row whose first field matches.
filtered_sheet() {
  local pat="$1" dest="$2"
  head -n1 "$INPUT" > "$dest"
  awk -F',' -v p="$pat" 'NR>1 {gsub(/\r/,"")} NR>1 && $1==p' "$INPUT" >> "$dest"
  if [[ $(wc -l < "$dest") -le 1 ]]; then
    echo "ERROR: no rows for patient '$pat' in $INPUT" >&2; return 1
  fi
}

# ---------------------------------------------------------------------------
# PASS ORDER IS A DEPENDENCY, NOT A PREFERENCE.
# A segmentation arm resumes from <root>/<from_arm>/csv/registered.csv, which
# does not exist until that registration arm has finished. Running the passes in
# plan order (or all concurrently) would race: the segmentation arms would fail
# on a missing checkpoint, and because conf/modules.config's errorStrategy has an
# 'ignore' branch, a partial tree can still look like a completed run.
# ---------------------------------------------------------------------------
pids=()
reap() {                          # block until under the concurrency cap
  while (( ${#pids[@]} >= CONCURRENCY )); do
    wait "${pids[0]}" || true
    pids=("${pids[@]:1}")
  done
}

run_pass() {
  local want_kind="$1"
  while IFS=',' read -r -a vals; do
    local kind; kind=$(col_val arm_kind "${vals[@]}")
    [[ "$kind" == "$want_kind" ]] || continue

    local run_id arm start stop from_arm from_csv only_patient
    run_id=$(col_val run_id "${vals[@]}")
    arm=$(col_val arm "${vals[@]}")
    start=$(col_val start "${vals[@]}")
    stop=$(col_val stop "${vals[@]}")
    from_arm=$(col_val from_arm "${vals[@]}")
    from_csv=$(col_val from_csv "${vals[@]}")
    only_patient=$(col_val only_patient "${vals[@]}")

    ARGS=()
    add_param start                "$start"
    add_param stop                 "$stop"
    add_param seg_method           "$(col_val seg_method "${vals[@]}")"
    add_param reg_qc               "$(col_val reg_qc "${vals[@]}")"
    add_param registration_method  "$(col_val registration_method "${vals[@]}")"
    add_param memory_mode          "$(col_val memory_mode "${vals[@]}")"
    add_param reg_micro_reg        "$(col_val reg_micro_reg "${vals[@]}")"
    # THE THREE reg_ashlar_* FLAGS ARE GONE. ashlar stopped being a pipeline backend at
    # :fire: 6a54479, so nextflow.config declares none of them and the schema would reject
    # all three. The external ashlar baseline is now driven by benchmarks/ashlar/ instead of
    # through this launcher; see the long note in build_arm_plan.py::_registration_arms.

    local in_csv="$INPUT"
    if [[ -n "$from_arm" ]]; then
      # Which checkpoint depends on WHERE this arm resumes: a registration arm
      # picks up csv/preprocessed.csv from the one shared preprocessing run, a
      # segmentation arm picks up csv/registered.csv from the arm it was told to
      # follow. Layout.checkpointCsvName() derives both names from the step
      # vocabulary, which is why they are the step name minus the "-ing".
      in_csv="$ROOT/$from_arm/csv/${from_csv}.csv"
      if [[ ! -f "$in_csv" ]]; then
        echo "[$run_id] SKIP: $in_csv missing — arm '$from_arm' did not complete" >&2
        continue
      fi
    elif [[ -n "$only_patient" ]]; then
      in_csv="$ROOT/.launch/$run_id.samplesheet.csv"
      mkdir -p "$(dirname "$in_csv")"
      filtered_sheet "$only_patient" "$in_csv" || continue
    fi

    reap
    launch "$run_id" "$arm" "$in_csv" "$ROOT/$arm" "${ARGS[@]}" &
    pids+=($!)
  done < <(tail -n +2 "$PLAN" | tr -d '\r')
}

# preprocess FIRST: every registration arm resumes from its csv/preprocessed.csv.
for kind in preprocess registration segmentation compute; do
  echo "=== pass: $kind ==="
  run_pass "$kind"
  # Barrier between passes: segmentation needs registration's checkpoint, and the
  # compute arm should not contend with the QC arms for nodes while it is being
  # timed — a cost measurement taken under self-inflicted contention is not the
  # cost of the pipeline.
  for p in "${pids[@]+"${pids[@]}"}"; do wait "$p" || true; done
  pids=()
done

echo
echo "All arms finished. Results under $ROOT"
echo "Label manifest: $ROOT/arms.csv (written by build_arm_plan.py --results-root)"
echo
echo "Next: pull the artifacts into ihc_method —"
echo "  benchmarks/pull_to_ihc_method.sh $ROOT ../ihc_method"
