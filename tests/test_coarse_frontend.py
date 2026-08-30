"""Tests for the COARSE global-alignment front-end dispatch in bin/utils/coarse_align.py.

Task 5.1: ``estimate_affine`` selects among ``FRONTENDS`` ("orb", "sift", "fourier_mellin",
"disk_lightglue"). The three CPU front-ends must recover a pure shift; the learned matcher must
raise a clear, actionable error when torch/kornia are unavailable rather than silently degrading.

Import note: coarse_align.py does ``from logger import get_logger`` at module scope (an
unqualified import resolved against ``bin/utils`` on sys.path directly, the same convention
tests/test_coarse_align.py and tests/test_tiled_coarse_thumbnail.py already use) -- so this
module is imported the same way those sibling test files do, rather than via
``bin.utils.coarse_align``, which would fail that inner import when this file is run alone.

Fixture note: the roll signs below are ``(-7, +5)``, not the brief's literal ``(+7, -5)``.
Verified against the untouched, pre-Task-5.1 ``estimate_rigid`` (git HEAD of this branch): with
``(+7, -5)`` the existing, must-stay-behaviour-preserving ORB implementation recovers
``(m0[0, 2], m0[1, 2]) == (+4.9, -7.05)`` -- the *opposite* sign of this file's assertions. Doc
string of ``estimate_rigid``/``_frontend_orb`` is unambiguous ("M0 mapping mov onto ref"), so the
implementation is correct and the brief's fixture had an inverted sign. Flipping the roll signs
here (keeping the assertion values exactly as specified) reproduces the intended pure-shift
recovery check without altering ORB's real, production transform convention.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
pytest.importorskip("skimage")

from coarse_align import FRONTENDS, estimate_affine


@pytest.fixture
def pair():
    rng = np.random.default_rng(0)
    ref = np.zeros((512, 512), dtype=np.float32)
    for _ in range(120):
        y, x = rng.integers(20, 492, 2)
        ref[y - 4:y + 4, x - 4:x + 4] = rng.uniform(0.5, 1.0)
    mov = np.roll(np.roll(ref, -7, axis=0), 5, axis=1)
    return ref, mov


@pytest.fixture
def rect_pair():
    """Non-square 256x512 fixture (rows x cols), same blob-fixture style as ``pair``.

    A SQUARE fixture cannot catch a (H, W) vs (W, H) ``image_size`` ordering bug: the two
    orderings are the same tuple when H == W. This one is 256 rows x 512 cols, and stays
    at or below 512 px in the larger dimension.
    """
    rng = np.random.default_rng(1)
    ref = np.zeros((256, 512), dtype=np.float32)
    for _ in range(120):
        y = rng.integers(20, 236)
        x = rng.integers(20, 492)
        ref[y - 4:y + 4, x - 4:x + 4] = rng.uniform(0.5, 1.0)
    mov = np.roll(np.roll(ref, -7, axis=0), 5, axis=1)
    return ref, mov


@pytest.mark.parametrize("frontend", ["orb", "sift", "fourier_mellin"])
def test_cpu_frontends_recover_a_pure_shift(pair, frontend):
    ref, mov = pair
    m0, info = estimate_affine(ref, mov, frontend=frontend)
    np.testing.assert_allclose(m0[0, 2], -5, atol=1.5)
    np.testing.assert_allclose(m0[1, 2], 7, atol=1.5)
    assert info["frontend"] == frontend


def test_unknown_frontend_is_rejected(pair):
    ref, mov = pair
    with pytest.raises(ValueError, match="unknown frontend"):
        estimate_affine(ref, mov, frontend="magic")


def test_disk_lightglue_is_declared_but_optional():
    assert "disk_lightglue" in FRONTENDS


def test_disk_lightglue_raises_a_clear_error_without_torch(pair, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_torch(name, *a, **k):
        if name.startswith(("torch", "kornia")):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_torch)
    ref, mov = pair
    with pytest.raises(RuntimeError, match="torch"):
        estimate_affine(ref, mov, frontend="disk_lightglue")


def test_disk_lightglue_recovers_a_pure_shift(pair):
    """DISK+LightGlue must recover the same shift the CPU front-ends do.

    Skipped when torch/kornia are absent. Task 2 adds them to CI's install, and
    tests/test_disk_test_actually_runs.py then asserts this test is NOT skipped --
    a skip that nobody notices is how an unimplemented front-end shipped once already.
    """
    pytest.importorskip("torch")
    pytest.importorskip("kornia")
    ref, mov = pair
    m0, info = estimate_affine(ref, mov, frontend="disk_lightglue")
    np.testing.assert_allclose(m0[0, 2], -5, atol=1.5)
    np.testing.assert_allclose(m0[1, 2], 7, atol=1.5)
    assert info["frontend"] == "disk_lightglue"
    assert info["n_inliers"] > 50, f"only {info['n_inliers']} inliers"
    assert np.isfinite(info["residual_px"]) and info["residual_px"] < 2.0


def test_disk_lightglue_recovers_a_pure_shift_on_a_rectangular_thumbnail(rect_pair):
    """Regression test for the ``image_size`` (H, W) vs (W, H) ordering bug.

    Every production thumbnail is non-square (``decimation_factor`` in
    bin/utils/tiled_io.py applies one shared integer factor and preserves aspect
    ratio), so the square ``pair`` fixture above cannot exercise this path -- it
    passed even when ``image_size`` was fed to LightGlue as ``t.shape[-2:]`` (H, W)
    instead of the (W, H) kornia's own fallback uses, which aborts on any real slide.
    """
    pytest.importorskip("torch")
    pytest.importorskip("kornia")
    ref, mov = rect_pair
    m0, info = estimate_affine(ref, mov, frontend="disk_lightglue")
    np.testing.assert_allclose(m0[0, 2], -5, atol=1.5)
    np.testing.assert_allclose(m0[1, 2], 7, atol=1.5)
    assert info["frontend"] == "disk_lightglue"
    assert info["n_inliers"] > 50, f"only {info['n_inliers']} inliers"
    assert np.isfinite(info["residual_px"]) and info["residual_px"] < 2.0
