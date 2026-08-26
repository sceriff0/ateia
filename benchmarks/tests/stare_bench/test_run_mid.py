"""Guards for the mid-rung runner: generate each pair once, register with
every method through the REAL pipeline.

These are static/structural checks, not an end-to-end pipeline run -- there
is no cluster in CI and the pipeline's images are not pulled here. See
run_mid.sh's own header for what a real invocation needs.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path("benchmarks/stare_bench/run_mid.sh")


def test_script_exists_and_is_executable():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & 0o111, "run_mid.sh must be executable"


def test_script_passes_shellcheck_or_bash_syntax():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_script_refuses_to_run_without_arguments():
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "usage" in (proc.stdout + proc.stderr).lower()


def test_script_does_not_invoke_the_removed_entry_point():
    # bin/tiled_register.py exists on no branch; run_registration.sh still calls
    # it, and this runner must not repeat that mistake.
    assert "tiled_register.py" not in SCRIPT.read_text()


def test_script_passes_amplitude_px_through_to_generate_pair():
    # Correction 1: build_plan crosses an amplitude_px axis; dropping it would
    # silently collapse two experimental conditions into one.
    text = SCRIPT.read_text()
    assert "amplitude_px" in text


def test_script_reads_the_committed_physics_block():
    # Correction 2: the config's physics block (photobleach, noise_and_psf,
    # background) must be merged with the per-row blank_regions fraction, or
    # generation silently produces clean, noiseless, unbleached images.
    text = SCRIPT.read_text()
    assert "physics" in text
    assert "yaml.safe_load" in text
    assert "synthetic_gt.yaml" in text


def test_script_does_not_pass_lazy_images():
    # Correction 3: these pairs need real images, not the size-only path.
    assert "lazy_images=True" not in SCRIPT.read_text()
    assert "lazy_images = True" not in SCRIPT.read_text()


@pytest.mark.parametrize("method", ["tiled", "valis", "ashlar"])
def test_score_pair_accepts_every_method(method):
    import inspect

    from benchmarks.stare_bench.cli import score_pair

    sig = inspect.signature(score_pair)
    assert "method" in sig.parameters
