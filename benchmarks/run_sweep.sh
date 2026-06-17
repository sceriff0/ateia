#!/usr/bin/env bash
# Drive a benchmark sweep: one pipeline launch per run_plan.csv row.
# Usage: benchmarks/run_sweep.sh <run_plan.csv> <matrix_manifest.csv> <results_root> [extra nextflow args...]
# Requires bash 4+ (associative arrays). On macOS the system /bin/bash is 3.x;
# install a modern bash (brew install bash) and invoke explicitly if needed.
set -euo pipefail

RUN_PLAN="${1:?run_plan.csv}"
MANIFEST="${2:?matrix_manifest.csv}"
ROOT="${3:?results root}"
shift 3
EXTRA=("$@")

mkdir -p "$ROOT"
header=$(head -n1 "$RUN_PLAN")
IFS=',' read -r -a cols <<< "$header"

# col_index <name>: print the 0-based index of column <name> in $cols[].
col_index() {
  local name="$1" i
  for i in "${!cols[@]}"; do
    [[ "${cols[$i]}" == "$name" ]] && echo "$i" && return
  done
  echo -1
}

# col_val <name> <vals_array_elements...>: extract value for named column.
# Usage: col_val run_id "${vals[@]}"
col_val() {
  local name="$1"; shift
  local vals=("$@")
  local idx
  idx=$(col_index "$name")
  if [[ "$idx" -ge 0 ]]; then echo "${vals[$idx]}"; else echo ""; fi
}

tail -n +2 "$RUN_PLAN" | while IFS=',' read -r -a vals; do
  run_id=$(col_val run_id "${vals[@]}")
  target_px=$(col_val target_px "${vals[@]}")
  n_channels=$(col_val n_channels "${vals[@]}")
  varied_axis=$(col_val varied_axis "${vals[@]}")
  cell_id="px${target_px}_ch${n_channels}"

  # Resolve this cell's image from the matrix manifest -> build a 1-row samplesheet.
  img=$(awk -F',' -v c="$cell_id" 'NR>1 && $1==c {print $7}' "$MANIFEST")
  if [[ -z "$img" ]]; then echo "WARN: no matrix cell $cell_id; skipping $run_id" >&2; continue; fi

  nch="${vals[$(col_index n_channels)]}"
  chans="DAPI"
  for ((i=1; i<nch; i++)); do chans+="|ch${i}"; done
  run_dir="$ROOT/$run_id"; mkdir -p "$run_dir"
  sheet="$run_dir/samplesheet.csv"
  printf 'patient_id,path_to_file,is_reference,channels\n%s,%s,true,%s\n' "$cell_id" "$img" "$chans" > "$sheet"

  # Map non-structural columns (skip run_id/varied_axis/target_px/n_channels) to --<param>.
  params=()
  for i in "${!cols[@]}"; do
    k="${cols[$i]}"
    case "$k" in run_id|varied_axis|target_px|n_channels) continue;; esac
    params+=("--${k}" "${vals[$i]}")
  done

  echo ">>> $run_id (varied=${varied_axis}, cell=$cell_id)"
  nextflow run . -profile docker \
    -c benchmarks/configs/benchmark.config \
    --input "$sheet" --outdir "$run_dir/out" --trace_dir "$run_dir/trace" \
    "${params[@]}" ${EXTRA[@]+"${EXTRA[@]}"} || echo "RUN FAILED: $run_id" >&2
done
