"""Registration QC must stop eagerly decoding the whole multi-channel slide.

``bin/utils/qc.py:create_registration_qc`` used to call ``tifffile.imread`` on the
*entire* reference and registered stacks (every marker channel, full resolution) just
to slice out the one nuclear/fiducial channel the overlay is built from. On a
gigapixel cyclic-IF slide with a dozen-plus marker channels, that is an order of
magnitude more data decoded than the QC step ever uses -- paid twice, once per slide.

``bin/utils/tiled_io.py`` already has the machinery to read one channel through a
lazy, region-readable zarr view (``open_lazy``) at a chosen decimation
(``decimation_factor`` + ``read_decimated``), proven correct by the STARE coarse-align
path (``tests/test_tiled_coarse_thumbnail.py::test_banded_read_is_numerically_identical_to_full_decimation``).
This file pins two properties for routing ``create_registration_qc`` through it:

1. The whole-stack eager read is gone -- both slides are opened through ``open_lazy``.
2. The result is EXACTLY what the old whole-stack read produced, not merely similar:
   same pixel values, same shape, same dtype, through the untouched
   ``create_nuclear_overlay``/``autoscale_for_display`` pipeline.

Property 2 does not fail against the pre-change code by construction: the pre-change
code already reads the correct channel via ``tifffile.imread``, so an "old vs. new"
pixel comparison built from the SAME resolved index is old-vs-old before the fix
lands, and only exercises the new code path's arithmetic once the fix is in. It is
included anyway as a regression guard against the real failure mode here -- silently
different pixels, not a crash -- and was falsified during development by temporarily
asserting the arrays were NOT equal (see task-1-report.md for the transcript), which
failed until the assertion was reverted, proving the comparison is not vacuous.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

UTILS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
)
if UTILS_DIR not in sys.path:
    sys.path.insert(0, UTILS_DIR)

pytest.importorskip("skimage")
pytest.importorskip("zarr")
tifffile = pytest.importorskip("tifffile")
cv2 = pytest.importorskip("cv2")

import qc  # noqa: E402
from metadata import extract_channel_names_from_ome, pick_nuclear_index  # noqa: E402


def _write_pair(tmp_path, h=96, w=80, channel_names=("SMA", "DAPI")):
    """A small 2-channel OME-TIFF pair with the nuclear marker deliberately NOT at
    index 0, so the test also exercises ``pick_nuclear_index`` rather than the
    channel-0 fallback CLAUDE.md warns is "correct only by accident".
    """
    rng = np.random.default_rng(1234)

    def _plane(seed):
        r = np.random.default_rng(seed)
        return r.integers(0, 65536, size=(h, w), dtype=np.uint16)

    ref = np.stack([_plane(1), _plane(2)]).astype(np.uint16)
    # registered image: same shape (post-registration slides share dimensions), different content
    reg = np.stack([_plane(3), _plane(4)]).astype(np.uint16)
    del rng

    ref_path = tmp_path / "reference.ome.tiff"
    reg_path = tmp_path / "registered.ome.tiff"
    tifffile.imwrite(
        str(ref_path),
        ref,
        ome=True,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": list(channel_names)}},
    )
    tifffile.imwrite(
        str(reg_path),
        reg,
        ome=True,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": list(channel_names)}},
    )
    return ref_path, reg_path


def test_registration_qc_never_eagerly_decodes_the_whole_stack(tmp_path, monkeypatch):
    """Both slides must be reached through ``open_lazy``, not a whole-array ``tifffile.imread``.

    Fails against the pre-change code, which has no ``open_lazy`` attribute on the ``qc``
    module at all -- this assertion cannot even be wired up until the routing lands.
    """
    ref_path, reg_path = _write_pair(tmp_path)
    out_path = tmp_path / "out" / "qc.tif"
    out_path.parent.mkdir()

    seen_paths = []
    orig_open_lazy = qc.open_lazy

    def spying_open_lazy(path):
        seen_paths.append(str(path))
        return orig_open_lazy(path)

    monkeypatch.setattr(qc, "open_lazy", spying_open_lazy)

    imread_full_calls = []
    orig_imread = tifffile.imread

    def spying_imread(path, *args, **kwargs):
        # A lazy zarr open still goes through tifffile.imread(..., aszarr=True); only a
        # plain call (no aszarr) actually materialises pixel data eagerly.
        if not kwargs.get("aszarr", False):
            imread_full_calls.append(str(path))
        return orig_imread(path, *args, **kwargs)

    monkeypatch.setattr(qc.tifffile, "imread", spying_imread)

    qc.create_registration_qc(
        reference_path=ref_path,
        registered_path=reg_path,
        output_path=out_path,
        scale_factor=0.25,
        save_fullres=True,
        save_png=True,
        save_tiff=True,
    )

    assert str(ref_path) in seen_paths, "reference slide never went through open_lazy"
    assert str(reg_path) in seen_paths, "registered slide never went through open_lazy"
    assert str(ref_path) not in imread_full_calls, (
        "reference slide was still eagerly decoded in full via tifffile.imread"
    )
    assert str(reg_path) not in imread_full_calls, (
        "registered slide was still eagerly decoded in full via tifffile.imread"
    )


def test_qc_overlay_pixels_match_the_old_whole_stack_read(tmp_path):
    """The decimated-read overlay must equal the old full-read overlay bit-for-bit.

    Builds the "old" reference the way ``create_registration_qc`` used to: a plain
    ``tifffile.imread`` of the whole stack, sliced at the ``pick_nuclear_index``-resolved
    channel, pushed through the untouched ``create_nuclear_overlay``. Compares that against
    what the real (patched) ``create_registration_qc`` actually wrote to disk.
    """
    ref_path, reg_path = _write_pair(tmp_path)

    ref_channels = extract_channel_names_from_ome(ref_path)
    reg_channels = extract_channel_names_from_ome(reg_path)
    ref_idx = pick_nuclear_index(ref_channels, None)
    reg_idx = pick_nuclear_index(reg_channels, None)
    assert ref_idx == 1 and reg_idx == 1, "fixture must exercise a non-zero nuclear index"

    old_ref_nuc = tifffile.imread(str(ref_path))[ref_idx]
    old_reg_nuc = tifffile.imread(str(reg_path))[reg_idx]
    expected_bgr, expected_cyx = qc.create_nuclear_overlay(
        old_ref_nuc, old_reg_nuc, scale_factor=0.25
    )

    out_path = tmp_path / "out" / "qc.tif"
    out_path.parent.mkdir()
    qc.create_registration_qc(
        reference_path=ref_path,
        registered_path=reg_path,
        output_path=out_path,
        scale_factor=0.25,
        save_fullres=False,
        save_png=True,
        save_tiff=True,
    )

    got_bgr = cv2.imread(str(out_path.with_suffix(".png")), cv2.IMREAD_UNCHANGED)
    got_cyx = tifffile.imread(str(out_path.with_suffix(".tif")))

    assert got_bgr.dtype == expected_bgr.dtype
    assert got_bgr.shape == expected_bgr.shape
    assert np.array_equal(got_bgr, expected_bgr)

    assert got_cyx.dtype == expected_cyx.dtype
    assert got_cyx.shape == expected_cyx.shape
    assert np.array_equal(got_cyx, expected_cyx)


def test_qc_fullres_output_matches_old_whole_stack_read(tmp_path):
    """The full-resolution TIFF (always produced in production) must also stay bit-exact."""
    ref_path, reg_path = _write_pair(tmp_path)

    ref_channels = extract_channel_names_from_ome(ref_path)
    reg_channels = extract_channel_names_from_ome(reg_path)
    ref_idx = pick_nuclear_index(ref_channels, None)
    reg_idx = pick_nuclear_index(reg_channels, None)

    old_ref_nuc = tifffile.imread(str(ref_path))[ref_idx]
    old_reg_nuc = tifffile.imread(str(reg_path))[reg_idx]
    ref_scaled = qc.autoscale_for_display(old_ref_nuc, method="minmax")
    reg_scaled = qc.autoscale_for_display(old_reg_nuc, method="minmax")
    expected_fullres = np.stack(
        [reg_scaled, ref_scaled, np.zeros_like(ref_scaled)], axis=0
    )

    out_path = tmp_path / "out" / "qc.tif"
    out_path.parent.mkdir()
    qc.create_registration_qc(
        reference_path=ref_path,
        registered_path=reg_path,
        output_path=out_path,
        scale_factor=0.25,
        save_fullres=True,
        save_png=False,
        save_tiff=False,
    )

    fullres_path = out_path.with_name(out_path.stem + "_fullres.tif")
    got_fullres = tifffile.imread(str(fullres_path))

    assert got_fullres.dtype == expected_fullres.dtype
    assert got_fullres.shape == expected_fullres.shape
    assert np.array_equal(got_fullres, expected_fullres)
