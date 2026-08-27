"""Guards for the mid-rung runner: generate each pair once, register with
every method through the REAL pipeline.

These are static/structural checks, not an end-to-end pipeline run -- there
is no cluster in CI and the pipeline's images are not pulled here. See
run_mid.sh's own header for what a real invocation needs.
"""

import subprocess
from pathlib import Path

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


def test_score_pair_accepts_a_method_argument():
    # Real per-method coverage (that a transform for "tiled"/"ashlar" is read
    # through the manifest schema, that "valis" reads a registrar pickle, and
    # that both derive the moving-slide name rather than assuming "mov")
    # lives in test_end_to_end.py, which can actually exercise score_pair
    # end to end. This check only guards the static signature this runner
    # depends on -- it was previously parametrized over methods it never
    # used, asserting the identical thing three times.
    import inspect

    from benchmarks.stare_bench.cli import score_pair

    sig = inspect.signature(score_pair)
    assert "method" in sig.parameters


def test_the_crop_source_is_read_from_the_config_not_hardcoded():
    """The runner hardcoded the generated-nuclei source, so the headline
    numbers -- whose whole justification is that reviewers discount synthetic
    texture -- would have been measured on generated nuclei.

    Asserts the CALL SITE is gone, not the identifier: the prose above it
    deliberately still names what it used to do.
    """
    text = SCRIPT.read_text()
    assert "SyntheticCropSource()" not in text
    assert "from_config" in text
    assert "crop_source=crop_source" in text


def test_the_crop_source_is_validated_once_before_generation():
    # A seed-selected slide list fails a SCATTERED SUBSET of the plan, not the
    # run -- so an undersized slide surfaces hundreds of rows deep unless the
    # whole set is checked upfront.
    text = SCRIPT.read_text()
    assert "crop_source.validate(" in text
    assert text.index("crop_source.validate(") < text.index("generate_pair(\n")


def test_the_pipeline_is_invoked_with_an_execution_profile():
    """`-profile singularity` alone sends every task to the LOCAL executor of
    whatever node the script runs on -- no SLURM submission, no cluster
    ceiling -- while still looking like a normal run.
    """
    text = SCRIPT.read_text()
    assert "-profile singularity \\" not in text, "bare singularity profile is back"
    assert 'NF_PROFILE="${NF_PROFILE:-singularity,slurm,ieo}"' in text
    assert '-profile "$NF_PROFILE"' in text


def test_gate_and_tre_artifacts_are_discovered_and_passed():
    # Without these the gate ROC -- the benchmark's one genuinely novel metric
    # -- is None for every row of a mid-rung run, wasting the compute.
    text = SCRIPT.read_text()
    assert "find_controls()" in text
    assert "find_tre()" in text
    assert "--controls" in text
    assert "--intrinsic-tre" in text
    assert "registered/controls" in text
    assert "qc/registration" in text


def test_gate_artifacts_are_looked_up_for_stare_only():
    """`_accept` is STARE's gate. Handing ASHLAR's row a controls directory
    would publish STARE's confusion matrix under ashlar's label -- score_pair
    refuses it, but the runner must not ask in the first place.
    """
    text = SCRIPT.read_text()
    for fn in ("find_controls()", "find_tre()"):
        body = text.split(fn, 1)[1].split("\n}", 1)[0]
        assert '"$method" != "tiled"' in body, f"{fn} is not gated on method"


def test_the_stale_never_published_claim_is_gone():
    """The VALIS registrar pickle IS published now (conf/modules.config's
    third REGISTER publishDir block). The comment claiming otherwise would
    send the next reader to "fix" a glob that already works -- and the
    obvious fix, anchoring on registered/, is NARROWER than the real
    two-level-deeper published path.
    """
    text = SCRIPT.read_text()
    assert "EXPECTED to find nothing today" not in text
    assert "registered/transform/preprocessed/data" in text
