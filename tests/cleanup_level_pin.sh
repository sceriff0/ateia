#!/usr/bin/env bash
# `cleanup_level` must reach the publish gates by EVERY route a param can arrive.
#
# WHY THIS IS A SCRIPT AND NOT A pytest OR AN nf-test
#
# The defect this guards against is an EVALUATION-ORDER one: a publish gate that
# is computed when conf/modules.config is parsed freezes against whatever
# `params.cleanup_level` is at that line, which is BEFORE `profiles {}` and before
# any `-c` file is merged. The resolved config then prints `cleanup_level = 'none'`
# while every gate is already false -- so a static guard over the sources, and
# `nextflow config`, both say the pin took. Only a run says otherwise. Measured
# 2026-09-06 on main @ c201f443: a `params { cleanup_level = 'none' }` pin passed
# via `-c site.config` -- the operator path README.md documents -- published 32
# files, identical to the shipped 'final'; the CLI flag published 64.
#
# Three legs, one assertion each, on the smallest observable: whether the
# `preprocessed/` intermediate holds a file. No file counts -- they drift with
# every added artifact and say nothing about the gate.
#
#   1. `-profile test` alone. conf/test.config pins 'none'; the intermediate
#      must be published on the strength of that pin.
#   2. `-c pin.config` setting 'none'. Same, by the documented site-config route.
#   3. `-c pin.config` setting 'final' OVER the test profile's 'none'. The
#      intermediate must NOT be published: a `-c` file outranks a profile on
#      params, and the gate must follow the winner, not the first value it saw.
#
# Usage: bash tests/cleanup_level_pin.sh
set -uo pipefail

cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

NF="${NEXTFLOW:-nextflow}"

fail() { echo "FAIL: $*"; exit 1; }

# The intermediate under assertion, at the depth checkpoint_manifest.nf.test
# pins: <outdir>/<patient>/preprocessed/. Same depth discipline as
# tests/cleanup_work.sh case 3 -- an unbounded -name also matches VALIS's own
# `preprocessed/data/` inside the ungated transform artifact.
intermediate_files() {
    local out="$1"
    find "$out" -mindepth 2 -maxdepth 2 -type d -name preprocessed 2>/dev/null \
      | while read -r d; do find "$d" -type f; done | wc -l | tr -d ' '
}

run_stub() {  # run_stub <label> <workdir> <outdir> [extra nextflow args...]
    local label="$1" w="$2" o="$3"; shift 3
    "$NF" -q run . -profile test -stub "$@" -w "$w" --outdir "$o" > "$TMP/$label.log" 2>&1 \
      || { cat "$TMP/$label.log"; fail "the '$label' run did not succeed"; }
}

# --------------------------------------------------------------------------
# 1. The test profile's own pin ('none') publishes the intermediate.
# --------------------------------------------------------------------------
run_stub profile "$TMP/w1" "$TMP/o1"
n=$(intermediate_files "$TMP/o1")
[ "$n" -gt 0 ] || fail "-profile test pins cleanup_level='none' but preprocessed/ holds no file: the gate did not see the profile"
echo "ok: -profile test (pins 'none') published $n preprocessed file(s)"

# --------------------------------------------------------------------------
# 2. A -c site-config pin ('none') publishes the intermediate.
# --------------------------------------------------------------------------
printf "params { cleanup_level = 'none' }\n" > "$TMP/pin_none.config"
run_stub c_none "$TMP/w2" "$TMP/o2" -c "$TMP/pin_none.config"
n=$(intermediate_files "$TMP/o2")
[ "$n" -gt 0 ] || fail "-c site.config pins cleanup_level='none' but preprocessed/ holds no file: the gate did not see the -c file"
echo "ok: -c pin ('none') published $n preprocessed file(s)"

# --------------------------------------------------------------------------
# 3. A -c pin ('final') over the profile's 'none' withholds the intermediate.
# --------------------------------------------------------------------------
printf "params { cleanup_level = 'final' }\n" > "$TMP/pin_final.config"
run_stub c_final "$TMP/w3" "$TMP/o3" -c "$TMP/pin_final.config"
n=$(intermediate_files "$TMP/o3")
[ "$n" -eq 0 ] || fail "-c site.config pins cleanup_level='final' over the profile's 'none' but preprocessed/ holds $n file(s): the gate followed the wrong layer"
echo "ok: -c pin ('final') over the profile published no preprocessed file"

echo "PASS"
