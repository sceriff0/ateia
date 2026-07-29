"""Regression test for bin.preprocess overlap handling.

`count_fovs` already accepted an `overlap` parameter, but
`apply_basic_correction` dropped the CLI-provided `--overlap` value into its
unused `**basic_kwargs` and never forwarded it to `count_fovs`, making the
knob a silent no-op. This test pins the fix: overlap == 0 (the default) must
keep today's FOV count unchanged, while overlap > 0 must increase it.
"""

import sys
import types

try:
    import basicpy  # noqa: F401
except ImportError:
    # basicpy (and its JAX-based autotune stack) isn't installed in every
    # local dev environment, but bin/preprocess.py imports it eagerly at
    # module scope. count_fovs itself has no BaSiC dependency, so stub just
    # enough of the module surface to import it and exercise the tiling
    # math directly. In CI/containers where basicpy is installed, this
    # stub is never used.
    stub = types.ModuleType("basicpy")

    class _StubBaSiC:
        def __init__(self, *args, **kwargs):
            pass

    stub.BaSiC = _StubBaSiC
    stub.__version__ = "0.0.0-stub"
    sys.modules["basicpy"] = stub

from bin.preprocess import count_fovs  # noqa: E402


def test_overlap_zero_matches_baseline_fov_count():
    """Default overlap=0 must be byte-identical to pre-fix behavior."""
    baseline = count_fovs((4000, 4000), (1950, 1950))
    assert baseline == (3, 3)
    assert count_fovs((4000, 4000), (1950, 1950), overlap=0) == baseline


def test_overlap_increases_fov_count():
    """Non-zero overlap shortens the tiling stride, so it must not decrease
    (and, for a large enough overlap, must increase) the FOV count."""
    baseline = count_fovs((4000, 4000), (1950, 1950), overlap=0)
    overlapped = count_fovs((4000, 4000), (1950, 1950), overlap=1500)
    assert overlapped[0] > baseline[0]
    assert overlapped[1] > baseline[1]
