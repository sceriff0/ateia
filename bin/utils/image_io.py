"""The one owner of TIFF writes in this pipeline.

Every ``bin/`` script that creates a TIFF goes through this module, and every write
here sets ``bigtiff=True``. ``tests/test_image_io_ownership.py`` enforces both halves
statically over all of ``bin/``, so a new script cannot reintroduce the defect class
by accident; ``tests/test_image_io_writer.py`` checks the bytes that come out.

Why bigtiff is not a parameter
------------------------------
``bin/tiled_stitch.py`` put it best, and its wording is the rule for this module:

    bigtiff is mandatory, not an optimisation: a registered slide is written
    uncompressed at full resolution, so classic TIFF's 32-bit offsets overflow
    (``struct.error: 'I' format requires 0 <= number <= 4294967295``) the moment the
    output crosses 4 GB -- C x H x W x itemsize, reached by any real WSI.

Before this module existed, five sites wrote >4 GB-capable data with no ``bigtiff``:
``segment.py``, ``segment_cellsam.py`` and ``segment_instantseg.py`` (the same
full-resolution label-mask write, copied across three segmentation backends),
``extract_mask_series.py`` (uint32 masks, and no compression either -- the worst of
them, since an uncompressed uint32 mask hits the 4 GB ceiling at a comparatively
small slide), and ``bin/utils/qc.py`` (whose *full-resolution* composite lacked it
while its *downsampled* sibling, in the same function, had it). Three backends
repeating one omission is not three bugs, it is a missing owner.

The write variants, and why each one exists
-------------------------------------------
The survey behind Task 11 found six mutually distinct write mechanisms. Four of them
are this module's entry points; the fifth was the defect class and no longer has a
way to be written; the sixth is not ours.

``write_ome_tiff``   -- single-shot OME-TIFF. The default, and what a consumer
                        expecting channel names and physical pixel size reads.
                        Used by ``convert_image.py``, ``preprocess.py`` and both
                        registration-QC composites in ``bin/utils/qc.py``.
``write_plain_tiff`` -- single-shot, deliberately *not* OME. Requires a stated
                        ``why_not_ome``, because "no OME header" is a design
                        decision with a reason, never a default that drifted in.
                        Used by ``split_multichannel.py``: an OME header on a
                        single-channel file would be a second source of channel
                        names for ``extract_channel_names_from_ome`` to find.
``write_mask_tiff``  -- a label mask. Non-OME on purpose (a label mask has no
                        markers to name, and consumers index it positionally), and
                        always compressed: label images are extremely
                        compressible and the uncompressed variant is what made
                        ``extract_mask_series.py`` the riskiest site in the repo.
``open_tiff_writer`` -- a ``TiffWriter`` for the two multi-write cases that cannot
                        be one call: ``tiled_stitch.py``'s streamed tile generator
                        (which never materialises the slide) and
                        ``merge_channels_pyramid.py``'s true multi-resolution
                        pyramid (base + ``subifds`` levels + an optional
                        un-pyramided mask series). Both keep full control of what
                        they write; this module owns only that the file is BigTIFF.

Not owned here: ``bin/register.py`` hands the entire registered-slide write to
VALIS's own writer (``slide_obj.warp_and_save_slide()``, pyvips-backed). None of the
properties above -- bigtiff, compression, tile shape, metadata convention -- are set
or even visible from this codebase for that one artifact. That is a known fact rather
than an invisible gap; see ``docs/image_io.md``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence

import tifffile

__all__ = [
    "write_ome_tiff",
    "write_plain_tiff",
    "write_mask_tiff",
    "open_tiff_writer",
]

# The pipeline's default compression. zlib (Adobe Deflate) is what every site that
# already compressed was using, so making it the default changes nothing on disk and
# gives new code the same answer without having to choose.
DEFAULT_COMPRESSION = "zlib"


def _prepared(path: str | Path) -> Path:
    """Coerce to Path and make sure the parent directory exists."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_ome_tiff(
    path: str | Path,
    data,
    *,
    metadata: Dict[str, Any],
    compression: Optional[str] = DEFAULT_COMPRESSION,
    tile: Optional[Sequence[int]] = None,
    photometric: str = "minisblack",
    resolution: Optional[Sequence[float]] = None,
    resolutionunit: Optional[str] = None,
) -> Path:
    """Write ``data`` as a single-resolution OME-TIFF.

    ``metadata`` is passed straight to tifffile's OME-XML generator, so it carries
    ``axes``, ``Channel``/``Name`` and ``PhysicalSize*`` exactly as the callers
    already built them -- this module does not reshape or rename anything, because
    channel order and dtype are part of the contract consumers read.

    ``compression=None`` writes uncompressed; that is not a recommendation, it is
    how ``convert_image.py`` has always written and this module does not change
    existing artifacts.
    """
    out = _prepared(path)
    kwargs: Dict[str, Any] = {
        "metadata": metadata,
        "photometric": photometric,
        "ome": True,
    }
    if compression is not None:
        kwargs["compression"] = compression
    if tile is not None:
        kwargs["tile"] = tuple(tile)
    if resolution is not None:
        kwargs["resolution"] = tuple(resolution)
    if resolutionunit is not None:
        kwargs["resolutionunit"] = resolutionunit

    tifffile.imwrite(str(out), data, bigtiff=True, **kwargs)
    return out


def write_plain_tiff(
    path: str | Path,
    data,
    *,
    why_not_ome: str,
    compression: Optional[str] = DEFAULT_COMPRESSION,
    tile: Optional[Sequence[int]] = None,
    photometric: Optional[str] = None,
    resolution: Optional[Sequence[float]] = None,
    resolutionunit: Optional[str] = None,
) -> Path:
    """Write ``data`` as a standard (non-OME) TIFF, with a stated reason.

    ``why_not_ome`` is required and must be non-empty. Omitting an OME header is a
    real design decision in this pipeline -- it is how ``split_multichannel.py``
    avoids creating a second, channel-less source of channel names -- and the way
    the pre-Task-11 defect class looked from the outside was precisely "no OME, no
    bigtiff, no reason". Making the reason a required argument means the variant
    stays reachable while the accident does not.

    ``ome=False`` is passed explicitly rather than left to tifffile's filename
    inference: tifffile writes OME-XML for any name ending ``.ome.tif``/``.ome.tiff``,
    so inference makes the header a property of the filename instead of the caller's
    intent.
    """
    if not isinstance(why_not_ome, str) or not why_not_ome.strip():
        raise ValueError(
            "write_plain_tiff(why_not_ome=...) must state why this file has no OME "
            "header; it is a documented variant, not a default."
        )
    out = _prepared(path)
    kwargs: Dict[str, Any] = {"ome": False}
    if compression is not None:
        kwargs["compression"] = compression
    if tile is not None:
        kwargs["tile"] = tuple(tile)
    if photometric is not None:
        kwargs["photometric"] = photometric
    if resolution is not None:
        kwargs["resolution"] = tuple(resolution)
    if resolutionunit is not None:
        kwargs["resolutionunit"] = resolutionunit

    tifffile.imwrite(str(out), data, bigtiff=True, **kwargs)
    return out


def write_mask_tiff(
    path: str | Path,
    mask,
    *,
    compression: str = DEFAULT_COMPRESSION,
) -> Path:
    """Write a segmentation label mask (2-D, or a ``(C, H, W)`` stack of planes).

    Non-OME by design: a label mask has no markers to name, and every consumer in
    this repo (``quantify.py``, ``mask_to_geojson.py``, ``extract_cell_properties.py``,
    ``export_spatialdata.py``) indexes it positionally. Compression is not optional:
    label images compress by an order of magnitude, and the one site that wrote them
    raw (``extract_mask_series.py``, uint32) reached classic TIFF's 4 GB offset limit
    sooner than any other write in the pipeline.

    The mask's dtype is written through unchanged -- label IDs are categorical, and
    a silent cast here would renumber cells.
    """
    out = _prepared(path)
    tifffile.imwrite(str(out), mask, bigtiff=True, ome=False, compression=compression)
    return out


@contextmanager
def open_tiff_writer(path: str | Path, *, ome: bool) -> Iterator[tifffile.TiffWriter]:
    """Yield a BigTIFF ``TiffWriter`` for the multi-write cases.

    Two callers, both of which genuinely cannot be a single ``imwrite``:

    - ``tiled_stitch.py`` streams a tile generator, so the slide is never
      materialised in RAM;
    - ``merge_channels_pyramid.py`` writes a base level with ``subifds`` reserved,
      one ``tif.write`` per pyramid level, and optionally a second series for the
      label masks (mean-downsampling those would corrupt label IDs, so that series
      is deliberately un-pyramided).

    Everything about *what* is written stays with the caller. This wrapper owns
    exactly one thing -- that the file is BigTIFF -- plus making ``ome`` explicit
    rather than inferred from the filename suffix, and closing the handle even when
    the body raises.
    """
    out = _prepared(path)
    writer = tifffile.TiffWriter(str(out), bigtiff=True, ome=ome)
    try:
        yield writer
    finally:
        writer.close()
