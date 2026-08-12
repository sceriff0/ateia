#!/usr/bin/env python3
"""The reg_qc=2 GeoJSON must not change when the QC stops segmenting for itself.

SEG_QC_GEOJSON used to run StarDist itself (``bin/segment_to_geojson.py``, now deleted)
and hand its in-memory ``cell_mask`` straight to
``mask_to_geojson.mask_to_feature_collection``. It now reads the ``*_cell_mask.tif``
that SEG_QC_SEGMENT -- ``SEGMENT`` under an alias -- wrote, and calls that same function
via ``bin/mask_to_geojson.py``'s CLI.

So the refactor is polygon-preserving IF AND ONLY IF the label mask survives the extra
TIFF round trip byte-for-byte. That is what this file pins, using the exact write call
``segment.run_segmentation`` makes (``tifffile.imwrite(..., compression="zlib")``).

WHAT THIS DOES NOT CLAIM. Equivalence holds *for the same backend*. Two things genuinely
change and are meant to:

  * At the shipped default (``seg_method='instantseg'``) the QC now segments with
    InstanSeg rather than StarDist, because it follows the run's own segmenter. Different
    segmenter, different cells -- that is the entire point of the change.
  * On the stardist backend the nuclear channel is now chosen positionally
    (``--dapi-channel 0``, guarded by ``SegBackends``' channel-0 check) rather than
    resolved from OME channel names. For CONVERT_IMAGE output -- which is everything that
    reaches this QC -- channel 0 IS the nuclear channel, so the resolved index agrees; the
    difference is that a mis-ordered slide now FAILS the guard instead of silently falling
    back to index 0 and scoring registration on an arbitrary marker.

``bin/segment.py`` imports StarDist/csbdeep/dask at module scope and is not importable
here, which is why the mask is synthetic rather than segmented.
"""

from __future__ import annotations

import json

import mask_to_geojson
import numpy as np
import pytest

# The one label dtype segment.py writes: `segment_nuclei` returns uint32 label images
# (bin/segment.py's docstring for it says so) and `run_segmentation` imwrites them as-is.
LABEL_DTYPE = np.uint32


def _synthetic_cell_mask() -> np.ndarray:
    """A label image with several non-trivial, non-convex blobs.

    Rectangles alone would be traced identically by any implementation and would make a
    round-trip check vacuous, so each blob gets a bite taken out of it: the contour then
    has real vertices for `approximate_polygon` to keep or drop, and a tolerance change
    is observable.
    """
    mask = np.zeros((120, 160), dtype=LABEL_DTYPE)
    for i, (r, c) in enumerate([(10, 10), (10, 70), (60, 30), (60, 100)], start=1):
        mask[r : r + 40, c : c + 45] = i
        mask[r + 5 : r + 20, c + 5 : c + 22] = 0  # concave notch
        mask[r + 30 : r + 40, c + 35 : c + 45] = 0  # clipped corner
    # A single-pixel label. Observed, not assumed: at tolerance <= 0.5 this survives as a
    # degenerate three-vertex, zero-area ring ([[150,110.5],[150,109.5],[150,110.5]]) --
    # `mask_to_feature_collection`'s `len(ring) < 3` filter does not catch it -- and at
    # tolerance >= 1.0 `approximate_polygon` collapses it and the filter does. Pre-existing
    # behaviour, identical on both sides of this refactor, and kept in the fixture because
    # a round-trip check should cover the ugly branch too.
    mask[110, 150] = 5
    return mask


def _write_like_segment_py(path, mask) -> None:
    """Write the mask exactly as ``segment.run_segmentation`` does."""
    import tifffile

    tifffile.imwrite(path, mask, compression="zlib")


@pytest.mark.parametrize("tolerance", [0.0, 0.5, 1.0, 2.0])
def test_mask_tiff_round_trip_preserves_every_polygon(tmp_path, tolerance):
    """New path (mask -> TIFF -> GeoJSON) == old path (mask -> GeoJSON), exactly."""
    mask = _synthetic_cell_mask()

    # The old path: bin/segment_to_geojson.py's final two statements, verbatim.
    expected = mask_to_geojson.mask_to_feature_collection(
        mask, simplify_tolerance=tolerance
    )

    # The new path: SEG_QC_SEGMENT writes the mask, SEG_QC_GEOJSON reads it back.
    mask_tif = tmp_path / "P001_slide1.ome_cell_mask.tif"
    _write_like_segment_py(mask_tif, mask)
    out = tmp_path / "P001_slide1.geojson"
    n = mask_to_geojson.write_geojson(mask_tif, out, simplify_tolerance=tolerance)

    actual = json.loads(out.read_text())

    assert actual == expected, "the TIFF round trip changed the traced geometry"
    assert n == len(expected["features"])

    # Non-vacuity: `{} == {}` would satisfy the equality above, so pin that the four real
    # cells are actually present AND that each is a real polygon rather than a sliver.
    by_label = {f["properties"]["label"]: f for f in actual["features"]}
    assert {1, 2, 3, 4} <= set(by_label), f"lost cells: {sorted(by_label)}"
    for label in (1, 2, 3, 4):
        ring = by_label[label]["geometry"]["coordinates"][0]
        assert len(ring) >= 5, f"cell {label} traced to a {len(ring)}-vertex sliver"


def test_the_written_label_image_is_bit_identical(tmp_path):
    """The equality above rests on this: zlib TIFF is lossless for uint32 labels.

    Asserted separately so a future change of compression or dtype in
    ``segment.run_segmentation`` fails HERE, naming the cause, rather than showing up as
    a mystery geometry diff.
    """
    import tifffile

    mask = _synthetic_cell_mask()
    path = tmp_path / "cell_mask.tif"
    _write_like_segment_py(path, mask)

    read_back = tifffile.imread(path)
    assert read_back.dtype == mask.dtype
    np.testing.assert_array_equal(read_back, mask)


def test_tolerance_actually_reaches_the_tracer(tmp_path):
    """--tolerance must not be silently dropped on the way through the CLI.

    conf/modules.config passes ``--tolerance ${params.simplify_tolerance}`` and nothing
    else now; if that flag stopped being threaded, every check above would still pass
    (they compare like-for-like at one tolerance), so pin that the value CHANGES the
    output. Guards the same class of bug as the original --tolerance omission, where the
    QC simplified at the script's own default while EXTRACT_CELL_PROPERTIES used the
    configured value.
    """
    mask = _synthetic_cell_mask()
    path = tmp_path / "cell_mask.tif"
    _write_like_segment_py(path, mask)

    loose = tmp_path / "loose.geojson"
    tight = tmp_path / "tight.geojson"
    mask_to_geojson.write_geojson(path, loose, simplify_tolerance=8.0)
    mask_to_geojson.write_geojson(path, tight, simplify_tolerance=0.0)

    assert json.loads(loose.read_text()) != json.loads(tight.read_text())


def test_cli_defaults_match_the_pipeline_tolerance(tmp_path, monkeypatch):
    """The CLI is now the ONLY way this conversion is reached, so drive it end to end."""
    import sys

    mask = _synthetic_cell_mask()
    path = tmp_path / "cell_mask.tif"
    _write_like_segment_py(path, mask)
    out = tmp_path / "slide.geojson"

    monkeypatch.setattr(
        sys,
        "argv",
        ["mask_to_geojson.py", "--mask", str(path), "--out", str(out), "--tolerance", "1.0"],
    )
    mask_to_geojson.main()

    assert json.loads(out.read_text()) == mask_to_geojson.mask_to_feature_collection(
        mask, simplify_tolerance=1.0
    )
