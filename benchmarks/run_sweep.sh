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
# tr -d '\r': tolerate CRLF-terminated CSVs (else the last column carries a trailing \r).
header=$(head -n1 "$RUN_PLAN" | tr -d '\r')
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

  # Pre-compute manifest column indices (done once, outside the loop).
manifest_header=$(head -n1 "$MANIFEST" | tr -d '\r')
IFS=',' read -r -a mcols <<< "$manifest_header"
manifest_col_index() {
  local name="$1" i
  for i in "${!mcols[@]}"; do
    [[ "${mcols[$i]}" == "$name" ]] && echo "$i" && return
  done
  echo -1
}
# 1-based awk field for the path column (always present).
path_col_idx=$(manifest_col_index "path")
path_awk_field=$((path_col_idx + 1))
moving_col_idx=$(manifest_col_index "moving_paths")
moving_awk_field=$((moving_col_idx + 1))

tail -n +2 "$RUN_PLAN" | tr -d '\r' | while IFS=',' read -r -a vals; do
  run_id=$(col_val run_id "${vals[@]}")
  target_px=$(col_val target_px "${vals[@]}")
  n_channels=$(col_val n_channels "${vals[@]}")
  varied_axis=$(col_val varied_axis "${vals[@]}")
  cell_id="px${target_px}_ch${n_channels}"

  # Resolve this cell's reference image from the matrix manifest.
  # gsub(/\r/,""): tolerate CRLF manifests so the last field has no trailing \r.
  img=$(awk -F',' -v c="$cell_id" -v f="$path_awk_field" 'NR>1 {gsub(/\r/,"")} $1==c {print $f}' "$MANIFEST")
  if [[ -z "$img" ]]; then echo "WARN: no matrix cell $cell_id; skipping $run_id" >&2; continue; fi

  # Resolve moving images: a ';'-separated list (empty when column absent or value empty).
  mov_list=""
  if [[ "$moving_col_idx" -ge 0 ]]; then
    mov_list=$(awk -F',' -v c="$cell_id" -v f="$moving_awk_field" 'NR>1 {gsub(/\r/,"")} $1==c {print $f}' "$MANIFEST")
  fi

  # Number of registration images for this run: 1 reference + (n_reg-1) moving panels.
  # Defaults to 2 (ref + 1 moving) when the axis is absent — the classic pair.
  n_reg=$(col_val n_register_images "${vals[@]}")
  [[ -z "$n_reg" ]] && n_reg=2

  nch="${vals[$(col_index n_channels)]}"
  # Build reference channel string: DAPI|ch1|...|ch{nch-1}
  ref_chans="DAPI"
  for ((i=1; i<nch; i++)); do ref_chans+="|ch${i}"; done

  run_dir="$ROOT/$run_id"; mkdir -p "$run_dir"
  sheet="$run_dir/samplesheet.csv"
  printf 'patient_id,path_to_file,is_reference,channels\n' > "$sheet"
  printf '%s,%s,true,%s\n' "$cell_id" "$img" "$ref_chans" >> "$sheet"

  # Emit up to (n_reg-1) moving panels, each with a distinct channel set
  # (DAPI|m{panel}_1|...) so the pipeline's duplicate-channel guard accepts them.
  if [[ -n "$mov_list" ]]; then
    IFS=';' read -r -a movings <<< "$mov_list"
    want=$(( n_reg - 1 )); emitted=0
    for ((j=0; j<${#movings[@]} && emitted<want; j++)); do
      panel=$(( j + 1 ))
      mov_chans="DAPI"
      for ((i=1; i<nch; i++)); do mov_chans+="|m${panel}_${i}"; done
      printf '%s,%s,false,%s\n' "$cell_id" "${movings[$j]}" "$mov_chans" >> "$sheet"
      emitted=$(( emitted + 1 ))
    done
    if [[ "$emitted" -lt "$want" ]]; then
      echo "WARN: $run_id wanted $want moving panels but only $emitted available for $cell_id" \
           "(regenerate the matrix with --n-moving >= $want)" >&2
    fi
  fi

  # Map non-structural columns to --<param>. Skip the matrix/registration-shape
  # controls (run_id/varied_axis/target_px/n_channels/n_register_images) — they are
  # consumed here to build the samplesheet, not passed to nextflow as pipeline params.
  params=()
  for i in "${!cols[@]}"; do
    k="${cols[$i]}"
    case "$k" in run_id|varied_axis|target_px|n_channels|n_register_images) continue;; esac
    params+=("--${k}" "${vals[$i]}")
  done

  echo ">>> $run_id (varied=${varied_axis}, cell=$cell_id)"
  nextflow run . -profile docker \
    -c benchmarks/configs/benchmark.config \
    --input "$sheet" --outdir "$run_dir/out" --trace_dir "$run_dir/trace" \
    "${params[@]}" ${EXTRA[@]+"${EXTRA[@]}"} || echo "RUN FAILED: $run_id" >&2
done
