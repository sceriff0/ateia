#!/usr/bin/env bash
# Work-directory cleanup and final-outputs-only publishing, end to end.
#
# WHY THIS IS A SCRIPT AND NOT A pytest OR AN nf-test
#
# `cleanup` is a Nextflow SESSION-TEARDOWN behaviour. It is not in the DAG, so no
# static guard over the sources can see it, and it fires after the pipeline has
# exited, so an nf-test's assertion context cannot observe it either -- nf-test
# hashes output files after teardown, which is exactly why conf/test.config has to
# pin `cleanup_work = false` for the rest of the suite to work at all. The only way
# to check this is to run the pipeline and look at the filesystem afterwards.
#
# Four properties, and the fourth is the one that matters most: cleanup must NEVER
# fire on a failed run, or a real failure becomes undiagnosable.
#
# WHAT "CLEANUP" ACTUALLY DOES -- measured, not assumed. Nextflow empties the task
# directories; it does not remove the tree. On the stub dataset a successful run
# goes from 404 files to 10 (collect-file scratch and work/tmp), with the empty
# two-character shells left behind. So case 1 counts FILES. An assertion on
# directories would fail against correct behaviour, which is the worst kind of
# guard to write.
#
# The test profile pins BOTH cleanup params away from the shipped defaults (see
# conf/test.config), so this script has to put them back. Via -params-file, never
# on the command line: Nextflow 26 delivers every CLI --param as a String, so a
# boolean passed that way is rejected by the schema as "[string] but should be
# [boolean]". docs/usage.md documents this for every boolean param, and
# tests/test_no_cli_boolean_params_in_docs.py enforces it across the repo.
#
# Usage: bash tests/cleanup_work.sh
set -uo pipefail

cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

NF="${NEXTFLOW:-nextflow}"

cat > "$TMP/shipped_defaults.json" <<'JSON'
{
  "cleanup_work": true,
  "cleanup_level": "final"
}
JSON

fail() { echo "FAIL: $*"; exit 1; }

# --------------------------------------------------------------------------
# 1. A SUCCESSFUL run empties the work directory.
# --------------------------------------------------------------------------
W="$TMP/w1"; O="$TMP/o1"
"$NF" -q run . -profile test -stub \
    -params-file "$TMP/shipped_defaults.json" \
    -w "$W" --outdir "$O" > "$TMP/run1.log" 2>&1 \
    || { cat "$TMP/run1.log"; fail "the baseline run did not succeed"; }

remaining=$(find "$W" -type f 2>/dev/null | wc -l | tr -d ' ')
# Not zero: collect-file scratch and work/tmp survive teardown by design. The
# threshold is "the task outputs are gone", and an uncleaned run leaves hundreds.
[ "$remaining" -lt 50 ] || fail "work dir still holds $remaining files after a successful run"
echo "ok: successful run left $remaining file(s) in work/"

# --------------------------------------------------------------------------
# 2. Final artifacts survive.
# --------------------------------------------------------------------------
for kind in pyramid geojson quantification; do
    find "$O" -type d -name "$kind" | grep -q . \
      || fail "final artifact '$kind' is missing"
done
find "$O" -type d -name 'qc' | grep -q . || fail "the QC tree is missing"
echo "ok: pyramid/ geojson/ quantification/ qc/ all present"

# --------------------------------------------------------------------------
# 3. Intermediates were never written.
# --------------------------------------------------------------------------
for kind in converted preprocessed split_channels quantify cell_properties segmentation; do
    if find "$O" -type d -name "$kind" -mindepth 2 | grep -q .; then
        if [ -n "$(find "$O" -type d -name "$kind" -mindepth 2 -exec find {} -type f \; 2>/dev/null)" ]; then
            fail "intermediate '$kind' was published at --cleanup_level=final"
        fi
    fi
done
echo "ok: no intermediate was published"

# --------------------------------------------------------------------------
# 3b. And the checkpoint directory says why it is empty.
# --------------------------------------------------------------------------
[ -f "$O/csv/README.txt" ] || fail "csv/README.txt was not written"
grep -q -- '--cleanup_level none' "$O/csv/README.txt" \
    || fail "csv/README.txt does not name the flag that restores the manifests"
[ -z "$(find "$O/csv" -name '*.csv' 2>/dev/null)" ] \
    || fail "a checkpoint manifest was written at --cleanup_level=final"
echo "ok: csv/ holds only the README"

# --------------------------------------------------------------------------
# 4. A FAILED run KEEPS its work directory. The evidence must survive.
# --------------------------------------------------------------------------
W2="$TMP/w2"; O2="$TMP/o2"
mkdir -p "$W2"
cat > "$TMP/fail.config" <<'CFG'
process { withName: 'CONVERT_IMAGE' { beforeScript = 'exit 1' } }
CFG
"$NF" -q run . -profile test -stub \
    -params-file "$TMP/shipped_defaults.json" \
    -c "$TMP/fail.config" -w "$W2" --outdir "$O2" > "$TMP/run2.log" 2>&1
rc=$?
[ "$rc" -ne 0 ] || fail "the forced-failure run exited 0"
kept=$(find "$W2" -type f 2>/dev/null | wc -l | tr -d ' ')
[ "$kept" -gt 0 ] || fail "work dir was emptied after a FAILED run -- the evidence is gone"
echo "ok: failed run (exit $rc) kept $kept file(s) in work/"

echo "PASS"
