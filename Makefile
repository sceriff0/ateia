# MIRAGE Pipeline - Test Runner
#
# Quick reference:
#   make test              → Fast stub + python tests (~5 min)
#   make test-real         → Real container tests (~15 min, needs Docker)
#   make test-integration  → Full end-to-end (~30+ min, needs Docker)
#   make test-all          → Everything
#
# Prerequisites:
#   pip install numpy tifffile pytest pytest-cov pandas scikit-image
#   nf-test installed (https://www.nf-test.com/)
#   Docker running (for real/integration tests)

.PHONY: testdata test test-stub test-real test-integration test-python test-validation test-lint test-all clean-test help

# Default target
test: test-stub test-python

help:
	@echo "MIRAGE Test Targets:"
	@echo "  make test              Quick: stub + python (~5 min)"
	@echo "  make test-stub         nf-test stub-only tests"
	@echo "  make test-real         nf-test real container tests (~15 min)"
	@echo "  make test-integration  Full integration tests (~30+ min)"
	@echo "  make test-python       Python unit tests (pytest)"
	@echo "  make test-validation   Input validation tests (dry-run)"
	@echo "  make test-lint         nf-core lint"
	@echo "  make test-all          Run everything"
	@echo "  make testdata          Generate test data"
	@echo "  make clean-test        Remove test artifacts"

# Generate test data (prerequisite for all test targets)
testdata:
	@echo "Generating test data..."
	python tests/testdata/generate_complete_testdata.py

# Tier 1: Fast stub tests — runs in CI on every push/PR (< 5 min)
test-stub: testdata
	nf-test test --profile test,docker --verbose

# Tier 2: Real execution tests with containers — runs in CI on main/dev push (~15 min)
test-real: testdata
	nf-test test --profile test,docker --tag real --verbose

# Tier 3: Full integration tests — manual from terminal only (~30+ min)
test-integration: testdata
	nf-test test --profile test,docker --tag integration --verbose

# Python unit tests — runs in CI on every push
test-python: testdata
	pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules --ignore=tests/subworkflows --ignore=tests/integration

# Validation tests (dry-run parameter validation) — runs in CI
test-validation: testdata
	bash tests/run_validation_tests.sh

# nf-core lint — runs in CI
test-lint:
	nf-core lint .

# Run everything
test-all: test-stub test-real test-integration test-python test-validation test-lint

# Cleanup test artifacts
clean-test:
	rm -rf .nf-test test_results test_results_stub test_results_full
	rm -rf work/ .nextflow.log* .nextflow/
	@echo "Test artifacts cleaned"
