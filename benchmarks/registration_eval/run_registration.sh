#!/usr/bin/env bash
# Register each pair twice (tiled on/off) with the standalone register.py, then
# evaluate TRE. Run where the VALIS environment / container is available.
# Usage: run_registration.sh <pairs_manifest.csv> <prepared_root> <results_root>
set -euo pipefail

MANIFEST="${1:?pairs_manifest.csv}"
PREPARED="${2:?prepared root (from prepare_pairs.py)}"
RESULTS="${3:?results root}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

mkdir -p "$RESULTS"
header=$(head -n1 "$MANIFEST"); IFS=',' read -r -a cols <<< "$header"
col() { local k="$1"; local i; for i in "${!cols[@]}"; do [ "${cols[$i]}" = "$k" ] && { echo "$i"; return; }; done; }
ci_pair=$(col pair_id); ci_mov=$(col moving_image); ci_ref=$(col ref_image)
ci_sl=$(col source_landmarks); ci_tl=$(col target_landmarks)
ci_w=$(col width); ci_h=$(col height); ci_ps=$(col pixel_size_um)

tail -n +2 "$MANIFEST" | while IFS=',' read -r -a v; do
  pair="${v[$ci_pair]}"; input_dir="$PREPARED/$pair/input"
  ref_name="$(basename "${v[$ci_ref]}")"; mov_name="$(basename "${v[$ci_mov]}")"
  mov_stem="${mov_name%%.*}"

  for mode in tiled untiled; do
    tiled_flag=""; [ "$mode" = "tiled" ] && tiled_flag="--use-tiled-registration"
    out_dir="$RESULTS/$pair/$mode"; mkdir -p "$out_dir"
    echo ">>> $pair [$mode]"
    python "$REPO_ROOT/bin/register.py" \
      --input-dir "$input_dir" --out "$out_dir/registered_slides" \
      --reference "$ref_name" $tiled_flag || { echo "REG FAILED: $pair $mode" >&2; continue; }

    pickle=$(find "$out_dir" -name "*_registrar.pickle" | head -1)
    summary=$(find "$out_dir" -name "*_summary.csv" | head -1)

    python -m benchmarks.registration_eval.eval_tre \
      --pickle "$pickle" --slide-name "$mov_stem" \
      --source-landmarks "${v[$ci_sl]}" --target-landmarks "${v[$ci_tl]}" \
      --width "${v[$ci_w]}" --height "${v[$ci_h]}" \
      ${v[$ci_ps]:+--pixel-size-um "${v[$ci_ps]}"} \
      ${summary:+--valis-summary "$summary"} \
      --mode "$mode" --pair-id "$pair" --out "$RESULTS/eval_${pair}_${mode}.json"
  done
done
echo "Done. Aggregate with: python -m benchmarks.registration_eval.aggregate_eval --eval-dir $RESULTS --out reg_eval.csv --agg-out reg_eval_agg.csv"
