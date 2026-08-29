#!/usr/bin/env bash
#
# Automated Validation Test Suite
# Tests input validation, error handling, and edge cases
#
# Based on test cases documented in tests/test_validation.md


SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTDATA_DIR="$SCRIPT_DIR/testdata"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/output/validation"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counters
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_TOTAL=0

echo "=========================================="
echo "MIRAGE Pipeline Validation Test Suite"
echo "=========================================="
echo ""

# Clean up previous test runs
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# `dry_run` is a `type: boolean` parameter in nextflow_schema.json and MUST NOT
# be passed on the command line.
#
#   * Nextflow 25.04.x turned `--dry_run true` (and a bare `--dry_run`) into a
#     real Boolean, so nf-schema accepted it.
#   * Nextflow 26.04.x hands EVERY `--param` through to nf-schema as a String —
#     `--dry_run true` AND a valueless `--dry_run` both arrive as the string
#     "true" — and validateParameters() rejects the run with
#         * --dry_run (true): Value is [string] but should be [boolean]
#     (measured on NXF_VER=26.04.6; a valueless flag is NOT a workaround).
#
# A `-params-file` carries a real JSON boolean, which is a different code path
# and is accepted unchanged by both engines. Any future boolean parameter this
# script needs must go in this file, never on the command line.
PARAMS_FILE="$OUTPUT_DIR/dry_run_params.json"
# dry_run exercises the whole validation surface without instantiating a process.
#
# pixel_size and the resource ceilings are here because the pipeline now REQUIRES them:
# pixel_size has no shipped default (a run must assert a scale or ask for 'auto', and these
# synthetic fixtures carry no OME PhysicalSize), and nf-schema's `required` list for
# resource_limits rejects a run that sets neither ceiling. Without them every case below
# dies at launch on the WRONG error -- "pass" cases look like failures, and "fail" cases
# fail on the missing ceilings instead of the samplesheet problem they are asserting on.
# The values are arbitrary but legal; nothing here is resource-bound, since dry_run means
# no process is ever instantiated.
#
# Written to a file rather than passed as --params: Nextflow 26 delivers every CLI --param
# as a String, so the CLI boolean form is rejected by the schema as "[string] but should be
# [boolean]".
printf '{ "dry_run": true, "pixel_size": 0.325, "max_cpus": 2, "max_memory": "6.GB" }\n' \
    > "$PARAMS_FILE"

# Helper function to run a test.
#
# Usage: run_test <name> <pass|fail> <input_csv> <expected_error> [extra nextflow args...]
#   <expected_error> is the substring required in the log for a "fail" test;
#   pass "" for "pass" tests. Keeping it as an explicit positional argument
#   (rather than the last element of the args array) avoids leaking the string
#   into the `nextflow run` command line.
run_test() {
    local test_name="$1"
    local expected_result="$2"  # "pass" or "fail"
    local input_csv="$3"
    local expected_error="$4"
    shift 4
    local extra_args=("$@")

    TESTS_TOTAL=$((TESTS_TOTAL + 1))
    echo -e "${YELLOW}[TEST $TESTS_TOTAL]${NC} $test_name"

    local output_file="$OUTPUT_DIR/test_${TESTS_TOTAL}.log"
    local exit_code=0

    # Run pipeline with dry_run for fast validation. `dry_run` comes from
    # $PARAMS_FILE, never the command line — see the comment where it is
    # written; a CLI boolean is rejected on Nextflow 26.
    # ${arr[@]+"${arr[@]}"} safely expands a possibly-empty array under `set -u`
    # (bash 3.2 on macOS errors on a bare "${arr[@]}" when empty).
    cd "$PROJECT_ROOT"
    nextflow run main.nf \
        -params-file "$PARAMS_FILE" \
        --input "$input_csv" \
        --outdir "$OUTPUT_DIR/test_${TESTS_TOTAL}" \
        ${extra_args[@]+"${extra_args[@]}"} \
        > "$output_file" 2>&1 || exit_code=$?

    if [ "$expected_result" = "pass" ]; then
        if [ $exit_code -eq 0 ]; then
            echo -e "${GREEN}✓ PASS${NC} - Validation succeeded as expected"
            TESTS_PASSED=$((TESTS_PASSED + 1))
            return 0
        else
            echo -e "${RED}✗ FAIL${NC} - Expected success but got failure"
            echo "  Exit code: $exit_code"
            echo "  Log: $output_file"
            TESTS_FAILED=$((TESTS_FAILED + 1))
            return 1
        fi
    else
        if [ $exit_code -ne 0 ]; then
            if grep -q "$expected_error" "$output_file"; then
                echo -e "${GREEN}✓ PASS${NC} - Failed with expected error: '$expected_error'"
                TESTS_PASSED=$((TESTS_PASSED + 1))
                return 0
            else
                echo -e "${RED}✗ FAIL${NC} - Failed but error message not found"
                echo "  Expected: '$expected_error'"
                echo "  Log: $output_file"
                TESTS_FAILED=$((TESTS_FAILED + 1))
                return 1
            fi
        else
            echo -e "${RED}✗ FAIL${NC} - Expected failure but validation passed"
            TESTS_FAILED=$((TESTS_FAILED + 1))
            return 1
        fi
    fi
}

echo "Generating test data..."
python3 "$TESTDATA_DIR/generate_complete_testdata.py" > /dev/null 2>&1

echo ""
echo "=========================================="
echo "Test Suite: Input Validation"
echo "=========================================="
echo ""

# Test 1.1: Valid input - one reference per patient
run_test \
    "Valid input - one reference per patient" \
    "pass" \
    "$TESTDATA_DIR/valid_preprocessing.csv" \
    "" \
    --start preprocessing

# Test 1.2: Invalid - multiple references per patient
run_test \
    "Invalid - multiple references per patient" \
    "fail" \
    "$TESTDATA_DIR/invalid_multi_ref.csv" \
    "Multiple reference images found" \
    --start preprocessing

# Test 1.3: Invalid - no reference per patient
run_test \
    "Invalid - no reference per patient" \
    "fail" \
    "$TESTDATA_DIR/invalid_no_ref.csv" \
    "No reference image found" \
    --start preprocessing

# Test 2.1: Valid - DAPI in a non-zero position (segmentation locates DAPI by
# name, so any position is accepted as long as DAPI is present).
run_test \
    "Valid - DAPI in non-zero position" \
    "pass" \
    "$TESTDATA_DIR/invalid_dapi_position.csv" \
    "" \
    --start preprocessing

# Test 2.2: Valid - DAPI in channel 0
run_test \
    "Valid - DAPI in channel 0" \
    "pass" \
    "$TESTDATA_DIR/valid_preprocessing.csv" \
    "" \
    --start preprocessing

# Test 2.3: Invalid - no nuclear channel at all.
#
# The rejection used to read "DAPI channel not found"; d5bcc06 deliberately
# generalised it when the nuclear-marker rule moved into lib/MarkerUtils.groovy
# and stopped hardcoding the literal 'DAPI' (a CELLTOX-only samplesheet is
# valid). The expectation below is a BRE, so the marker list inside the
# parentheses can change without this test drifting again — but it still
# asserts the nuclear-channel reason and the offending patient, not merely
# "the run failed".
run_test \
    "Invalid - no nuclear channel" \
    "fail" \
    "$TESTDATA_DIR/invalid_no_dapi.csv" \
    "No nuclear channel (.*) found for patient P001" \
    --start preprocessing

# Test 2.4: Invalid - input file does not exist
run_test \
    "Invalid - input file not found" \
    "fail" \
    "$TESTDATA_DIR/invalid_file_not_found.csv" \
    "does not exist" \
    --start preprocessing

# Test 2.5: Invalid - empty samplesheet (header only)
run_test \
    "Invalid - empty samplesheet" \
    "fail" \
    "$TESTDATA_DIR/empty_samplesheet.csv" \
    "no data rows" \
    --start preprocessing

echo ""
echo "=========================================="
echo "Test Suite: Checkpoint CSV Validation"
echo "=========================================="
echo ""

# Test 6.1: Valid checkpoint CSV (registration)
run_test \
    "Valid checkpoint CSV for registration step" \
    "pass" \
    "$TESTDATA_DIR/valid_checkpoint_registration.csv" \
    "" \
    --start registration

# Test 6.2: Invalid - missing required column
run_test \
    "Invalid checkpoint - missing required column" \
    "fail" \
    "$TESTDATA_DIR/invalid_checkpoint_missing_col.csv" \
    "Missing required column" \
    --start registration

# Test 6.3: Invalid - malformed is_reference value
run_test \
    "Invalid checkpoint - malformed is_reference" \
    "fail" \
    "$TESTDATA_DIR/invalid_checkpoint_bad_ref.csv" \
    "Invalid is_reference value" \
    --start registration

echo ""
echo "=========================================="
echo "Test Results Summary"
echo "=========================================="
echo ""
echo "Total tests: $TESTS_TOTAL"
echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
echo -e "${RED}Failed: $TESTS_FAILED${NC}"
echo ""

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}=========================================="
    echo "All validation tests passed! ✓"
    echo -e "==========================================${NC}"
    exit 0
else
    echo -e "${RED}=========================================="
    echo "Some tests failed! ✗"
    echo -e "==========================================${NC}"
    echo ""
    echo "Check logs in: $OUTPUT_DIR"
    exit 1
fi
