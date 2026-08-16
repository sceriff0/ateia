"""Shared single-channel extraction for the segmentation backends.

``segment.py`` (StarDist) and ``segment_cellsam.py`` (CellSAM) both need one
channel -- the nuclear/DAPI channel -- out of a multichannel OME-TIFF before
segmenting it. That read lives here, separate from both backends, for two
reasons:

1. It stays testable without either backend's heavy ML stack
   (stardist/csbdeep/tensorflow for ``segment.py``; cellSAM/torch for
   ``segment_cellsam.py``, both pulled in only inside those modules). This
   module's own imports are numpy, tifffile, and -- via ``tiled_io`` -- zarr,
   the same lightweight footprint ``tiled_io.py`` itself has.
2. The two backends get one implementation instead of two copies that can
   silently drift apart.

Reads via ``tiled_io.open_lazy`` (a tifffile zarr view), NOT
``tifffile.TiffFile(...).asarray(out="memmap")``. The latter's name is
misleading: on compressed input it does not memory-map the source file at
all. It decodes the ENTIRE image into a brand-new, full-size, *uncompressed*
temp file and memory-maps THAT -- verified directly (the returned
``numpy.memmap``'s backing file is a ``tmp*.memmap``, not the source
``.tif``). So "extracting only the DAPI channel" out of that array happens
only after every channel has already been decoded and spilled to scratch at
full size: on a 20 GB slide, 20 GB of temp disk and a full decode, to use one
plane of it.

``open_lazy``'s zarr view decodes lazily on region access instead, so
indexing a single channel only decodes that channel's tiles -- no full-image
decode, no full-size temp file.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import tifffile
from numpy.typing import NDArray
from tiled_io import open_lazy
from validation import clip_negative_values

__all__ = ["extract_dapi_channel"]


def extract_dapi_channel(
    multichannel_image_path: str,
    dapi_channel_index: int = 0,
    *,
    logger: Optional[logging.Logger] = None,
) -> Tuple[NDArray, Dict]:
    """Extract one channel from a multichannel OME-TIFF via a lazy zarr read.

    Parameters
    ----------
    multichannel_image_path : str
        Path to multichannel OME-TIFF file (e.g., from VALIS registration).
    dapi_channel_index : int, optional
        Index of the channel to extract. Default is 0 (first channel).
    logger : logging.Logger, optional
        Logger to log progress to. Defaults to this module's logger.

    Returns
    -------
    dapi_image : ndarray, shape (Y, X)
        The extracted channel, negative-clipped (see ``clip_negative_values``).
    metadata : dict
        Image metadata from OME-TIFF (``{"ome": <xml str>}`` if present).

    Raises
    ------
    ValueError
        If the image has unexpected dimensions (not 2-D or 3-D), or
        ``dapi_channel_index`` is out of range for a 3-D (C, Y, X) image.
    """
    log = logger or logging.getLogger(__name__)
    log.info(f"Loading multichannel image: {multichannel_image_path}")

    # Shape/dtype/OME metadata only -- no pixel data decoded here.
    with tifffile.TiffFile(multichannel_image_path) as tif:
        if tif.series:
            source = tif.series[0]
        else:
            # Non-OME TIFF or corrupted OME metadata - fall back to pages
            log.warning("No OME series found, falling back to raw pages")
            if not tif.pages:
                raise ValueError(
                    f"TIFF file appears corrupted: {multichannel_image_path}"
                )
            source = tif.pages[0]

        image_shape = source.shape
        image_dtype = source.dtype

        metadata: Dict = {}
        if hasattr(tif, "ome_metadata") and tif.ome_metadata:
            metadata["ome"] = tif.ome_metadata
            log.info("  ✓ OME metadata found")

        log.info(f"  Image shape: {image_shape}")
        log.info(f"  Image dtype: {image_dtype}")

    if len(image_shape) not in (2, 3):
        raise ValueError(
            f"Unexpected image dimensions: {image_shape}. "
            f"Expected 2D (Y, X) or 3D (C, Y, X)"
        )

    # Lazy read: only the requested channel's tiles are decoded (see module
    # docstring for why this replaces tif.asarray(out="memmap")).
    lazy_view, _dtype, close = open_lazy(multichannel_image_path)
    try:
        if len(image_shape) == 2:
            # Single channel image, assume it's DAPI
            log.info("  - Single channel image (assuming DAPI)")
            dapi_image = np.array(lazy_view[0, :, :], copy=True)
        else:
            n_channels = image_shape[0]
            log.info(f"  - Multichannel image with {n_channels} channels")

            if dapi_channel_index >= n_channels:
                raise ValueError(
                    f"DAPI channel index {dapi_channel_index} out of range "
                    f"for image with {n_channels} channels"
                )

            # Only this channel's tiles are decoded -- the whole-image decode
            # tif.asarray(out="memmap") would trigger never happens.
            log.info(
                f"  - Extracting DAPI channel (index {dapi_channel_index}) "
                f"- memory efficient (only this channel's tiles are decoded)"
            )
            dapi_image = np.array(lazy_view[dapi_channel_index, :, :], copy=True)
            log.info(f"  - Extracted DAPI channel (index {dapi_channel_index})")
    finally:
        close()

    dapi_image = clip_negative_values(dapi_image, log, stage_name="extract_dapi")

    log.info(f"  DAPI channel shape: {dapi_image.shape}")
    log.info(f"  DAPI dtype: {dapi_image.dtype}")
    log.info(f"  DAPI value range: [{dapi_image.min()}, {dapi_image.max()}]")

    return dapi_image, metadata
