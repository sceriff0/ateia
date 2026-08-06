#!/usr/bin/env bash
# Register each pair with BOTH registration methods, then evaluate TRE against the
# ground-truth landmarks. Run where the VALIS environment / container is available.
#
# Usage: run_registration.sh <pairs_manifest.csv> <prepared_root> <results_root> [stardist_model_dir]
#
# Methods (these are the pipeline's two real registration_method values):
#   valis  -> bin/register.py         : VALIS registrar pickle + its own *_summary.csv (rTRE)
#   tiled  -> bin/tiled_register.py   : STARE transform manifest + its intrinsic *_tre.json
#
# Passing <stardist_model_dir> additionally runs the pipeline's reg_qc=2 segmentation-overlap
# QC per run (segment both slides to GeoJSON, then score dice/displacement through the
# transform), giving the landmark-free accuracy signal alongside the ground truth. Omit it to
# skip that leg — the landmark TRE and the method-native numbers do not depend on it.
set -euo pipefail

MANIFEST="${1:?pairs_manifest.csv}"
PREPARED="${2:?prepared root (from prepare_pairs.py)}"
RESULTS="${3:?results root}"
SEG_MODEL_DIR="${4:-}"
SEG_MODEL_NAME="${SEG_MODEL_NAME:-stardist_full_e200_lr00001_aug1_seed10_es50p0.001_rlr0.5p50}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

mkdir -p "$RESULTS"
header=$(head -n1 "$MANIFEST"); IFS=',' read -r -a cols <<< "$header"
col() { local k="$1"; local i; for i in "${!cols[@]}"; do [ "${cols[$i]}" = "$k" ] && { echo "$i"; return; }; done; }
ci_pair=$(col pair_id); ci_mov=$(col moving_image); ci_ref=$(col ref_image)
ci_sl=$(col source_landmarks); ci_tl=$(col target_landmarks)
ci_w=$(col width); ci_h=$(col height); ci_ps=$(col pixel_size_um)

# Segment one native slide to a cell GeoJSON (same script SEG_QC_GEOJSON runs in-pipeline).
seg_geojson() {
  local image="$1" out="$2"
  python "$REPO_ROOT/bin/segment_to_geojson.py" \
    --image "$image" --output "$out" \
    --model-dir "$SEG_MODEL_DIR" --model-name "$SEG_MODEL_NAME"
}

tail -n +2 "$MANIFEST" | while IFS=',' read -r -a v; do
  pair="${v[$ci_pair]}"; input_dir="$PREPARED/$pair/input"
  ref_name="$(basename "${v[$ci_ref]}")"; mov_name="$(basename "${v[$ci_mov]}")"
  mov_stem="${mov_name%%.*}"; ref_stem="${ref_name%%.*}"

  # reg_qc=2 needs both slides segmented in their NATIVE frame; done once per pair and
  # reused by both methods, since the segmentation is transform-independent.
  ref_gj=""; mov_gj=""
  if [ -n "$SEG_MODEL_DIR" ]; then
    ref_gj="$RESULTS/$pair/${ref_stem}_cells.geojson"
    mov_gj="$RESULTS/$pair/${mov_stem}_cells.geojson"
    mkdir -p "$RESULTS/$pair"
    seg_geojson "$input_dir/$ref_name" "$ref_gj" || { echo "SEG FAILED (ref): $pair" >&2; ref_gj=""; }
    [ -n "$ref_gj" ] && { seg_geojson "$input_dir/$mov_name" "$mov_gj" \
      || { echo "SEG FAILED (moving): $pair" >&2; ref_gj=""; mov_gj=""; }; }
  fi

  for method in valis tiled; do
    out_dir="$RESULTS/$pair/$method"; mkdir -p "$out_dir"
    echo ">>> $pair [$method]"

    transform=""; summary=""; stare_tre=""
    if [ "$method" = "valis" ]; then
      python "$REPO_ROOT/bin/register.py" \
        --input-dir "$input_dir" --out "$out_dir/registered_slides" \
        --reference "$ref_name" || { echo "REG FAILED: $pair $method" >&2; continue; }
      transform=$(find "$out_dir" -name "*_registrar.pickle" | head -1)
      summary=$(find "$out_dir" -name "*_summary.csv" | head -1)
    else
      python "$REPO_ROOT/bin/tiled_register.py" \
        --reference "$input_dir/$ref_name" --moving "$input_dir/$mov_name" \
        --out "$out_dir/${mov_stem}_registered.ome.tiff" \
        --manifest "$out_dir/${mov_stem}_manifest.json" \
        --tre-summary "$out_dir/${mov_stem}_tre.json" \
        || { echo "REG FAILED: $pair $method" >&2; continue; }
      transform="$out_dir/${mov_stem}_manifest.json"
      stare_tre="$out_dir/${mov_stem}_tre.json"
    fi
    [ -n "$transform" ] || { echo "NO TRANSFORM: $pair $method" >&2; continue; }

    # reg_qc=2 segmentation-overlap QC through this method's transform.
    seg_qc=""
    if [ -n "$ref_gj" ] && [ -n "$mov_gj" ]; then
      seg_qc="$out_dir/${mov_stem}_seg_qc.json"
      python "$REPO_ROOT/bin/warp_seg_qc.py" \
        --method "$method" --pickle "$transform" \
        --ref-slide "$ref_stem" --moving-slide "$mov_stem" \
        --ref-geojson "$ref_gj" --moving-geojson "$mov_gj" \
        --output "$seg_qc" --patient-id "$pair" \
        ${v[$ci_ps]:+--pixel-size-um "${v[$ci_ps]}"} \
        || { echo "SEG QC FAILED: $pair $method" >&2; seg_qc=""; }
    fi

    python -m benchmarks.registration_eval.eval_tre \
      --pickle "$transform" --slide-name "$mov_stem" --method "$method" \
      --source-landmarks "${v[$ci_sl]}" --target-landmarks "${v[$ci_tl]}" \
      --width "${v[$ci_w]}" --height "${v[$ci_h]}" \
      ${v[$ci_ps]:+--pixel-size-um "${v[$ci_ps]}"} \
      ${summary:+--valis-summary "$summary"} \
      ${stare_tre:+--stare-tre "$stare_tre"} \
      ${seg_qc:+--seg-qc "$seg_qc"} \
      --mode "$method" --pair-id "$pair" --out "$RESULTS/eval_${pair}_${method}.json"
  done
done
echo "Done. Aggregate with: python -m benchmarks.registration_eval.aggregate_eval --eval-dir $RESULTS --out reg_eval.csv --agg-out reg_eval_agg.csv"
