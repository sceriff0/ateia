#!/usr/bin/env python3
"""Cell segmentation using CellSAM on the nuclear (DAPI) channel.

CellSAM (https://github.com/vanvalenlab/cellSAM) is a SAM-based foundation
model for cell segmentation. ``cellsam_pipeline`` returns a *single* instance
label map; it does not separately predict nuclei vs whole-cell.

This backend is wired to behave like the StarDist backend (``segment.py``):
- it segments the nuclear (DAPI) channel only -- CellSAM is NOT channel
  invariant (it expects <=3 channels ordered ``(blank, nuclear, whole-cell)``),
  so we place DAPI in the nuclear slot and leave the whole-cell slot empty;
- CellSAM's instance map is treated as the ``nuclei`` mask, and the
  whole-cell ``cell`` mask is derived by expanding the nuclei labels with
  ``skimage.segmentation.expand_labels`` (``--expand-distance``), exactly as
  StarDist does. This keeps the two backends behaviourally aligned.

Drop-in I/O contract with ``segment.py`` and ``segment_instantseg.py``:
- writes ``{prefix}_nuclei_mask.tif`` and ``{prefix}_cell_mask.tif`` as
  uint32 instance labels (compression='zlib')
- emits ``versions.yml`` (cellSAM + torch + python) -- written by the module
- emits ``{prefix}.SEGMENT.size.csv`` (input-size trace row) -- written by
  the module

DAPI extraction is shared with the StarDist backend via ``bin/utils/segment_io.py``,
a module with no ML dependencies of its own -- importing it here does NOT pull in
``segment.py``'s ``stardist``/``tensorflow``, which stay absent from the CellSAM
container. Tiled label expansion (``expand_labels_tiled``) is still *replicated*
below rather than imported from ``segment.py``, for the same isolation reason:
``segment.py`` itself imports ``stardist``/``tensorflow`` at top level.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Tuple

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / "utils"))

import dask.array as da
import numpy as np
import tifffile
from image_utils import ensure_dir
from logger import configure_logging, get_logger
from numpy.typing import NDArray
from segment_io import extract_dapi_channel as _extract_dapi_channel_impl
from skimage import segmentation

logger = get_logger(__name__)

#: TIFF tile size (px) for the nuclei/cell mask writes below -- a TIFF LAYOUT choice, not a
#: processing tile size. See ``bin/convert_image.py:35-49`` for why an untiled write forces
#: every downstream region read to decode the whole plane. Deliberately half of
#: CONVERT_TIFF_TILE/WRITE_TILE's 2048, not copied from it: measured directly on the mask's
#: own read pattern (many small windowed per-cell/per-region reads, not whole-plane decodes)
#: -- see docs/perf/2026-08-26-rss.md's tiled-vs-striped table.
MASK_TIFF_TILE = 1024


def extract_dapi_channel(
    multichannel_image_path: str,
    dapi_channel_index: int = 0,
) -> NDArray:
    """Extract a single (DAPI/nuclear) channel from a multichannel OME-TIFF.

    Delegates to ``segment_io.extract_dapi_channel`` (``bin/utils/segment_io.py``),
    which reads through ``tiled_io.open_lazy`` -- a tifffile zarr view that decodes
    only the tiles a slice actually touches, so only the requested channel is ever
    decoded. This is a lazy zarr read, NOT a memory-map of the source file: unlike
    what ``tif.asarray(out="memmap")`` implies, that call decodes the ENTIRE image
    into a new full-size *uncompressed* temp file on compressed input before any
    slicing happens. Negative interpolation artefacts (e.g. bicubic overshoot from
    registration) are clipped to zero by ``segment_io``. Shares its implementation
    with ``segment.py:extract_dapi_channel`` -- ``segment_io`` has no stardist/
    tensorflow or cellSAM/torch imports, so it stays lightweight for both backends.

    Parameters
    ----------
    multichannel_image_path
        Path to multichannel OME-TIFF (e.g. registration output).
    dapi_channel_index
        Index of the nuclear channel. Default 0 (DAPI in channel 0).

    Returns
    -------
    NDArray, shape (Y, X)
        The extracted nuclear channel.
    """
    dapi_image, _metadata = _extract_dapi_channel_impl(
        multichannel_image_path, dapi_channel_index, logger=logger
    )
    return dapi_image


def expand_labels_tiled(
    label_image: NDArray,
    distance: int = 1,
    tile_size: int = 1024,
) -> NDArray:
    """Parallel tiled label expansion using Dask.

    Identical to ``segment.py:expand_labels_tiled``: uses ``dask.array.map_overlap``
    so peak memory scales with the tile rather than the whole image, while
    producing the same result as a full-image ``expand_labels``.

    Parameters
    ----------
    label_image : NDArray, shape (Y, X)
        Label image with background=0 and labels>=1.
    distance
        Distance (pixels) to expand labels.
    tile_size
        Tile side length for chunked processing.

    Returns
    -------
    NDArray
        Expanded label image, same shape as input.
    """
    overlap = distance + 1

    if tile_size <= 2 * overlap:
        return segmentation.expand_labels(label_image, distance=distance)

    dask_labels = da.from_array(label_image, chunks=tile_size)

    def _expand_tile(tile: NDArray) -> NDArray:
        return segmentation.expand_labels(tile, distance=distance).astype(np.uint32)

    expanded = da.map_overlap(
        _expand_tile,
        dask_labels,
        depth=overlap,
        boundary="none",
        dtype=np.uint32,
    )

    return expanded.compute()


def _build_cellsam_input(dapi_image: NDArray) -> NDArray:
    """Lay out the nuclear channel as CellSAM's ``(H, W, 3)`` input.

    CellSAM expects multiplexed data ordered ``(blank, nuclear, whole-cell)``
    with the whole-cell slot optional. We place DAPI in the nuclear slot and
    leave blank + whole-cell as zeros (nuclear-only segmentation). This matches
    CellSAM's training layout for fluorescence-nuclear data better than passing
    a bare 2D grayscale array (which CellSAM may treat as brightfield).
    """
    blank = np.zeros_like(dapi_image)
    whole_cell = np.zeros_like(dapi_image)
    cellsam_input = np.stack([blank, dapi_image, whole_cell], axis=-1)
    logger.info(
        f"  Built CellSAM input (blank, nuclear, whole-cell): "
        f"shape={cellsam_input.shape}, dtype={cellsam_input.dtype}"
    )
    return cellsam_input


def run_cellsam(
    image_path: str,
    output_dir: str,
    dapi_channel_index: int = 0,
    expand_distance: int = 10,
    use_gpu: bool = False,
    use_wsi: bool = True,
    bbox_threshold: float = 0.4,
    block_size: int = 400,
    overlap: int = 56,
    model_path: str | None = None,
    prefix: str | None = None,
) -> Tuple[str, str]:
    """Run the CellSAM segmentation pipeline on the nuclear channel.

    Parameters
    ----------
    image_path
        Path to multichannel OME-TIFF (e.g. registration output).
    output_dir
        Directory where ``*_nuclei_mask.tif`` / ``*_cell_mask.tif`` are written.
    dapi_channel_index
        Index of the nuclear (DAPI) channel. Default 0.
    expand_distance
        Distance (px) to expand CellSAM nuclei labels into the whole-cell mask
        (StarDist-style). Default 10.
    use_gpu
        Whether GPU is requested. ``cellsam_pipeline`` selects the device
        internally; this flag is informational and warned about if CUDA is
        unavailable.
    use_wsi
        Enable CellSAM's native whole-slide tiling (recommended for large
        registered images).
    bbox_threshold
        CellSAM bounding-box confidence threshold (precision/recall knob).
    block_size
        Tile side length (px) for WSI mode. Upstream-recommended range [256, 2048].
    overlap
        Tile overlap (px) for WSI mode.
    model_path
        Path to pre-downloaded CellSAM weights. If None, ``cellsam_pipeline``
        auto-downloads via ``get_model`` (needs ``DEEPCELL_ACCESS_TOKEN``).
    prefix
        Output filename prefix. Defaults to the input image's stem.
    """
    logger.info("=" * 80)
    logger.info("CELL SEGMENTATION PIPELINE (CellSAM)")
    logger.info("=" * 80)
    logger.info(f"Input image: {image_path}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Nuclear (DAPI) channel index: {dapi_channel_index}")
    logger.info(f"Cell expansion distance: {expand_distance}px")
    logger.info(f"Use GPU (requested): {use_gpu}")
    logger.info(f"use_wsi: {use_wsi}  block_size: {block_size}  overlap: {overlap}")
    logger.info(f"bbox_threshold: {bbox_threshold}")
    logger.info(f"model_path: {model_path or '(auto-download)'}")
    logger.info("")

    ensure_dir(output_dir)

    # Defer heavy imports until after argparse/help works without GPU stacks.
    from cellSAM import cellsam_pipeline  # type: ignore

    # CUDA probe. cellsam_pipeline has NO device argument -- it auto-selects via
    # `device = 'cuda' if torch.cuda.is_available() else 'cpu'`. So if GPU is
    # requested but CUDA is not visible inside the container, the entire WSI
    # would silently run on CPU (multi-day runs). Fail loudly instead: a hard
    # error in seconds beats a 4-day CPU crawl that nobody notices.
    if use_gpu:
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "--use-gpu requested but torch.cuda.is_available() is False. "
                "CellSAM would silently fall back to CPU (multi-day WSI runs). "
                "Check that the GPU is bound into the container: Docker needs "
                "'--gpus all', Singularity needs '--nv', and the host must have "
                "the NVIDIA Container Toolkit / drivers installed. To deliberately "
                "run on CPU, launch the pipeline with seg_gpu=false."
            )
        logger.info(f"  CUDA available: {torch.cuda.get_device_name(0)}")

    # 1. Extract DAPI channel and build CellSAM's (H, W, 3) input.
    dapi_image = extract_dapi_channel(image_path, dapi_channel_index)
    cellsam_input = _build_cellsam_input(dapi_image)
    del dapi_image

    # 2. Run cellsam_pipeline -> single instance label map (the nuclei mask).
    # use_wsi enables CellSAM's native tiling so peak memory is bounded by
    # block_size rather than full image dimensions; required for WSI inputs.
    logger.info("Running cellsam_pipeline...")
    seg_start = time.time()
    nuclei_labels = cellsam_pipeline(
        cellsam_input,
        model_path=model_path,
        bbox_threshold=bbox_threshold,
        use_wsi=use_wsi,
        block_size=block_size,
        overlap=overlap,
    )
    logger.info(f"  Segmentation time: {time.time() - seg_start:.2f}s")
    del cellsam_input

    # CellSAM returns a 2D uint32 instance map (0=background). Ensure uint32.
    nuclei_mask = np.asarray(nuclei_labels).astype(np.uint32, copy=False)

    # 3. Derive whole-cell mask by expanding nuclei labels (StarDist-style).
    logger.info(
        f"Expanding nuclei labels to create cell mask (distance={expand_distance}px)..."
    )
    cell_mask = expand_labels_tiled(nuclei_mask, distance=expand_distance).astype(
        np.uint32
    )

    n_nuclei = int(nuclei_mask.max())
    n_cells = int(cell_mask.max())
    logger.info(f"  Nuclei detected: {n_nuclei}")
    logger.info(f"  Cells (after expansion): {n_cells}")

    # 4. Save outputs (mirror segment.py naming).
    basename = prefix if prefix else Path(image_path).stem
    nuclei_mask_path = Path(output_dir) / f"{basename}_nuclei_mask.tif"
    cell_mask_path = Path(output_dir) / f"{basename}_cell_mask.tif"

    logger.info("")
    logger.info("Saving segmentation masks...")
    logger.info(f"  Nuclei mask: {nuclei_mask_path.name}")
    # bigtiff for the same reason tiled_stitch.py:118 and extract_mask_series.py give:
    # a 40000x40000 uint32 label mask is 6.4 GB before compression, and classic TIFF's
    # 32-bit offsets overflow past 4 GB. Compression usually keeps it under -- usually is
    # not a contract. Guarded by tests/test_slide_io_seam.py.
    tifffile.imwrite(
        nuclei_mask_path,
        nuclei_mask,
        compression="zlib",
        bigtiff=True,
        tile=(MASK_TIFF_TILE, MASK_TIFF_TILE),
    )
    del nuclei_mask

    logger.info(f"  Cell mask: {cell_mask_path.name}")
    tifffile.imwrite(
        cell_mask_path,
        cell_mask,
        compression="zlib",
        bigtiff=True,
        tile=(MASK_TIFF_TILE, MASK_TIFF_TILE),
    )
    del cell_mask

    logger.info("")
    logger.info("=" * 80)
    logger.info("✓ CellSAM SEGMENTATION COMPLETE")
    logger.info("=" * 80)

    return str(nuclei_mask_path), str(cell_mask_path)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Cell segmentation using CellSAM on the nuclear (DAPI) channel",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--image",
        type=str,
        required=True,
        help="Path to multichannel OME-TIFF image (e.g., registered image from VALIS)",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Output directory for segmentation masks",
    )

    parser.add_argument(
        "--dapi-channel",
        type=int,
        default=0,
        help="Index of the nuclear (DAPI) channel in the multichannel image",
    )

    parser.add_argument(
        "--expand-distance",
        type=int,
        default=10,
        help="Distance (pixels) to expand nuclei labels for the whole-cell mask",
    )

    parser.add_argument(
        "--use-gpu",
        action="store_true",
        default=False,
        help="Request GPU (cellsam_pipeline picks the device automatically; informational)",
    )

    parser.add_argument(
        "--use-wsi",
        action="store_true",
        default=False,
        help="Enable CellSAM native WSI tiling (recommended for large images)",
    )

    parser.add_argument(
        "--bbox-threshold",
        type=float,
        default=0.4,
        help="CellSAM bounding-box confidence threshold (precision/recall knob)",
    )

    parser.add_argument(
        "--block-size",
        type=int,
        default=1024,
        help="Tile block size (px) for WSI mode",
    )

    parser.add_argument(
        "--overlap",
        type=int,
        default=56,
        help="Tile overlap (px) for WSI mode",
    )

    parser.add_argument(
        "--model-path",
        type=str,
        default=None,
        help="Path to pre-downloaded CellSAM weights. If omitted, weights are "
        "auto-downloaded (requires DEEPCELL_ACCESS_TOKEN).",
    )

    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="Output filename prefix (defaults to input image stem)",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    configure_logging(level=logging.INFO)
    args = parse_args()

    run_cellsam(
        image_path=args.image,
        output_dir=args.output_dir,
        dapi_channel_index=args.dapi_channel,
        expand_distance=args.expand_distance,
        use_gpu=args.use_gpu,
        use_wsi=args.use_wsi,
        bbox_threshold=args.bbox_threshold,
        block_size=args.block_size,
        overlap=args.overlap,
        model_path=args.model_path,
        prefix=args.prefix,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
