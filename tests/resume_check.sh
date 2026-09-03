#!/usr/bin/env bash
# Behavioural counterpart to tests/test_resume_determinism.py: does an identical
# rerun actually reuse the cache?
#
# WHY THIS IS A SCRIPT AND NOT PART OF THE BLOCKING GATE
#
# It measures an OUTCOME, and the regression it looks for is intermittent: the
# groupTuple arrival-order bug this suite was built around showed up in roughly one
# run in four, so a single pass proves little and even N passes only bound the
# probability. tests/test_resume_determinism.py asserts the ORDERING MECHANISM is
# present instead, which cannot pass by luck -- that is the check CI gates on.
# This script is what you run after touching a fan-in, a collectFile, or anything a
# process script reads, and it is how the numbers in
# docs/superpowers/specs/2026-08-10-resume-determinism-design.md were produced.
#
# Expected: every run reports CACHED=26 RESUBMITTED=2, and the 2 are exactly
# GENERATE_QC_REPORT and AGGREGATE_SIZE_LOGS -- both declared `cache = false` in
# conf/modules.config because their inputs are collectFile() results, which are
# rewritten to a fresh work/tmp/<hash>/ every run and so can never cache under
# Nextflow's default path+size+mtime hashing.
#
# Usage: tests/resume_check.sh [runs]     (default 3)

cd "$(dirname "$0")/.."

RUNS="${1:-3}"
WORK="$(mktemp -d)/w"
OUT="$(mktemp -d)/out"
EXPECTED_UNCACHEABLE=("GENERATE_QC_REPORT" "AGGREGATE_SIZE_LOGS")

cleanup() { rm -rf "$(dirname "$WORK")" "$(dirname "$OUT")"; }
trap cleanup EXIT

echo "baseline run..."
# UNCHECKED USED TO MEAN VACUOUS. A failed baseline (e.g. no `nextflow` on PATH,
# or a crash before any process runs) previously fell straight through into the
# resume loop with an EMPTY log: `names` derived from a log that was never
# produced is empty, the per-name loop below never executes, `status` never
# leaves 0, and the script prints "OK" having checked nothing. Reproduced with a
# fake `nextflow` that `exit 1`s: CACHED=0 RESUBMITTED=0 [] ... OK ... exit 0.
nextflow -q run . -profile test -stub -w "$WORK" --outdir "$OUT" >/dev/null \
    || { echo "FAIL: baseline run did not complete" >&2; exit 1; }

status=0
for i in $(seq 1 "$RUNS"); do
    log=$(nextflow run . -profile test -stub -w "$WORK" --outdir "$OUT" -resume -ansi-log false 2>&1) \
        || { echo "FAIL: resume $i did not complete" >&2; echo "$log" >&2; exit 1; }
    # `|| cached=0`, not `|| true`: `grep -c` exits 1 to mean ZERO MATCHES, which
    # here is data rather than an error -- a resume that cached nothing is exactly
    # what this script is looking for. Written as the assignment it actually is, so
    # that the file contains no `|| true` for a reader to have to classify.
    cached=$(grep -c 'Cached process'   <<<"$log") || cached=0
    rerun=$(grep -c 'Submitted process' <<<"$log") || rerun=0
    names=$(grep 'Submitted process' <<<"$log" | sed 's/.*> //' | sed 's/ (.*//' | sed 's/.*://' | sort -u | tr '\n' ' ')
    echo "resume $i: CACHED=$cached RESUBMITTED=$rerun  [$names]"

    # A resume that completed but cached NOTHING is not a pass by omission: this
    # pipeline's stub run always has cacheable processes upstream of the two
    # declared-uncacheable reports, so CACHED=0 on a successful resume means the
    # cache was never consulted (e.g. a fresh -w, or a hash that changes every
    # run) rather than that nothing needed re-running.
    if [[ "$cached" -eq 0 ]]; then
        echo "  FAIL: resume $i cached nothing (CACHED=0). A successful resume of" >&2
        echo "        this pipeline can never report CACHED=0 -- either the work" >&2
        echo "        directory was not actually reused, or every task's hash changed." >&2
        status=1
    fi

    for n in $names; do
        if [[ ! " ${EXPECTED_UNCACHEABLE[*]} " =~ [[:space:]]${n}[[:space:]] ]]; then
            echo "  FAIL: $n re-ran but is not one of the declared-uncacheable reports." >&2
            echo "        An identical rerun must reuse it. Something reaching it is ordered by" >&2
            echo "        arrival time, or its script references a value that changes per run." >&2
            echo "        Diagnose with: nextflow run ... -resume -dump-hashes json, twice, and diff" >&2
            echo "        that process's entries." >&2
            status=1
        fi
    done
done

[[ $status -eq 0 ]] && echo "OK: only the declared-uncacheable reports re-ran."
exit $status
