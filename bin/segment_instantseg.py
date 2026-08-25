#!/usr/bin/env python3
"""Cell segmentation using InstanSeg on multichannel images.

Channel-invariant alternative to StarDist on DAPI. InstanSeg's pretrained
``fluorescence_nuclei_and_cells`` model consumes the full multichannel image
and emits both nuclei and whole-cell instance masks in one pass.

Drop-in I/O contract with ``segment.py``:
- writes ``{prefix}_nuclei_mask.tif`` and ``{prefix}_cell_mask.tif`` as
  uint32 instance labels (compression='zlib')
- emits ``versions.yml`` (instanseg + torch + python)
- emits ``{prefix}.SEGMENT.size.csv`` (input-size trace row)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np
import tifffile
from image_utils import ensure_dir
from logger import configure_logging, get_logger

logger = get_logger(__name__)


def _to_numpy(arr):
    """Convert torch tensor or numpy-like object to numpy ndarray.

    InstanSeg may return either a numpy array or a torch tensor depending on
    version/device. This shim handles both without importing torch directly.
    """
    if hasattr(arr, "detach"):
        # torch tensor — move to CPU first
        try:
            arr = arr.detach().cpu().numpy()
        except Exception:
            arr = arr.cpu().numpy() if hasattr(arr, "cpu") else arr.numpy()
    elif hasattr(arr, "numpy"):
        arr = arr.numpy()
    return np.asarray(arr)


def _extract_2d_masks(labeled_output, target: str):
    """Reduce InstanSeg's labeled output to a (nuclei_2d, cell_2d) pair.

    InstanSeg's ``eval_small_image`` returns instance labels with shape that
    varies by ``target``. Common observed shapes:
      - target='all_outputs': (1, 2, Y, X) or (2, Y, X) — index 0 = nuclei, index 1 = cells
      - target='nuclei':      (1, 1, Y, X) or (1, Y, X) or (Y, X) — single nuclei map
      - target='cells':       (1, 1, Y, X) or (1, Y, X) or (Y, X) — single cell map

    This routine is defensive: it strips leading singleton dims, then dispatches
    by remaining ndim.

    A single label map is REFUSED, not replicated into both outputs. See the
    comment on that branch: replication preserves the file contract and voids
    the data contract, because Cytoplasm = Cell - Nucleus is then empty for
    every cell and every cytoplasmic measurement is silently zero. So of the
    three targets above, only 'all_outputs' can satisfy this reader -- which is
    what nextflow.config already ships as the default.
    """
    arr = _to_numpy(labeled_output)
    logger.info(f"  InstanSeg raw output shape: {arr.shape}, dtype: {arr.dtype}")

    # Strip leading singleton dims (e.g. batch axis)
    while arr.ndim > 3 and arr.shape[0] == 1:
        arr = arr[0]

    # ONE LABEL MAP IS NOT TWO MASKS.
    #
    # This branch used to `return single, single` -- replicating the one map
    # into both outputs and calling it "contract-preserving". It preserves the
    # FILE contract (both _nuclei_mask.tif and _cell_mask.tif get written) and
    # destroys the DATA contract: downstream computes
    # Cytoplasm = Cell - Nucleus, so an identical pair makes the cytoplasm
    # EMPTY FOR EVERY CELL. Every cytoplasmic column in measurements.csv comes
    # out zero and no output anywhere looks wrong.
    #
    # `--target nuclei` and `--target cells` produce a single map by
    # construction, so they can never satisfy this reader. nextflow.config
    # defaults seg_instantseg_target to 'all_outputs', so no shipped path
    # relied on the replication -- which is what makes refusing safe.
    if arr.ndim == 2 or (arr.ndim == 3 and arr.shape[0] == 1):
        raise ValueError(
            f"InstanSeg returned a SINGLE label map (shape {arr.shape}) for "
            f"target={target!r}, but this pipeline needs two distinct masks: "
            f"it derives Cytoplasm = Cell - Nucleus, which would be empty for "
            f"every cell if the nuclei and cell masks were the same array, and "
            f"every cytoplasmic measurement would silently be zero. "
            + (
                "Use --target all_outputs, which returns both."
                if target in ("nuclei", "cells")
                else "This target is supposed to return both, so the selected "
                "--model cannot produce cell masks. Choose one that can, such "
                "as fluorescence_nuclei_and_cells."
            )
        )

    if arr.ndim == 3:
        n = arr.shape[0]
        if n >= 2:
            # InstanSeg eval_small_image with target='all_outputs' returns a
            # tensor of shape (2, Y, X): index 0 = nuclei labels, index 1 =
            # cell labels.
            # Ref: instanseg/inference_class.py upstream and the README usage
            # examples at https://github.com/instanseg/instanseg.
            nuclei_2d = arr[0]
            cell_2d = arr[1]
            return nuclei_2d, cell_2d

    raise ValueError(
        f"Unexpected InstanSeg output shape {arr.shape}; "
        f"expected 3D (N, Y, X) with N >= 2 -- N < 2 is reported above as the "
        f"single-label-map case, which is a different problem with a different "
        f"remedy."
    )


def run_instanseg(
    image_path: str,
    output_dir: str,
    model_name: str = "fluorescence_nuclei_and_cells",
    target: str = "all_outputs",
    pixel_size: float | None = None,
    use_gpu: bool = False,
    prefix: str | None = None,
    tile_size: int = 512,
    batch_size: int = 1,
):
    """Run the InstanSeg segmentation pipeline on a multichannel image.

    Uses ``eval_medium_image`` (sliding-window tiled inference) so that peak
    activation memory scales with ``tile_size`` and ``batch_size`` rather than
    with the full image dimensions. ``eval_small_image`` was previously used
    and is unsuitable for WSI-scale registered outputs (OOM).

    Parameters
    ----------
    image_path
        Path to multichannel OME-TIFF (e.g. registration output).
    output_dir
        Directory where ``*_nuclei_mask.tif`` / ``*_cell_mask.tif`` are written.
    model_name
        Name of the pretrained InstanSeg model. Default is
        ``'fluorescence_nuclei_and_cells'``.
    target
        InstanSeg ``eval`` target. In practice only ``'all_outputs'`` works
        here: ``'cells'`` and ``'nuclei'`` produce a single label map, and a
        single map is REFUSED rather than written to both
        ``_nuclei_mask.tif`` and ``_cell_mask.tif``. Replicating it used to be
        described as contract-preserving; it preserves the file contract and
        voids the data contract, since Cytoplasm = Cell - Nucleus is then
        empty for every cell.
    pixel_size
        Override pixel size in µm/px. If None, InstanSeg's auto-detected value
        from the OME metadata is used.
    use_gpu
        Whether GPU is requested. InstanSeg picks a device internally; this
        flag is informational and warned about if CUDA is unavailable.
    prefix
        Output filename prefix. Defaults to the input image's stem.
    tile_size
        Side length (px) of square sliding-window tiles for inference.
    batch_size
        Number of tiles pushed through the model per forward pass. Higher
        values better utilise large GPUs; reduce on memory-constrained ones.
    """
    logger.info("=" * 80)
    logger.info("CELL SEGMENTATION PIPELINE (InstanSeg)")
    logger.info("=" * 80)
    logger.info(f"Input image: {image_path}")
    logger.info(f"Output directory: {output_dir}")
    logger.info(f"Model: {model_name}")
    logger.info(f"Target: {target}")
    logger.info(f"Pixel size override: {pixel_size}")
    logger.info(f"Use GPU (requested): {use_gpu}")
    logger.info("")

    ensure_dir(output_dir)

    # Defer heavy imports until after argparse/help works without GPU stacks
    from instanseg import InstanSeg  # type: ignore

    # Check CUDA availability if GPU requested (informational only — InstanSeg
    # routes device internally)
    if use_gpu:
        try:
            import torch

            if not torch.cuda.is_available():
                logger.warning(
                    "  --use-gpu requested but torch.cuda.is_available() is False; "
                    "InstanSeg will fall back to CPU."
                )
            else:
                logger.info(f"  CUDA available: {torch.cuda.get_device_name(0)}")
        except Exception as exc:
            logger.warning(f"  Could not query torch CUDA state: {exc}")

    # 1. Load model
    start_time = time.time()
    logger.info("Loading InstanSeg model...")
    model = InstanSeg(model_name, verbosity=1)
    logger.info(f"  Model load time: {time.time() - start_time:.2f}s")

    # 2. Read multichannel image (InstanSeg handles channel-invariance)
    logger.info("Reading image via InstanSeg...")
    image_array, detected_pixel_size = model.read_image(image_path)
    logger.info(
        f"  Image array dtype: {getattr(image_array, 'dtype', type(image_array))}"
    )
    logger.info(f"  Detected pixel size: {detected_pixel_size}")

    effective_pixel_size = pixel_size if pixel_size is not None else detected_pixel_size
    logger.info(f"  Effective pixel size used: {effective_pixel_size}")

    # 3. Run segmentation via tiled sliding-window inference.
    # eval_medium_image keeps peak memory bounded by tile_size * batch_size
    # rather than by full image dimensions; required for WSI-scale inputs.
    # Ref: instanseg/inference_class.py eval_medium_image signature.
    logger.info(
        f"Running InstanSeg eval_medium_image "
        f"(target={target}, tile_size={tile_size}, batch_size={batch_size})..."
    )
    seg_start = time.time()
    labeled_output, _image_tensor = model.eval_medium_image(
        image_array,
        effective_pixel_size,
        tile_size=tile_size,
        batch_size=batch_size,
        target=target,
        normalise=True,
        return_image_tensor=True,
        rescale_output=True,
    )
    logger.info(f"  Segmentation time: {time.time() - seg_start:.2f}s")

    # 4. Unpack outputs into 2D nuclei + cell masks
    nuclei_2d, cell_2d = _extract_2d_masks(labeled_output, target=target)

    # Cast to uint32 — downstream (extract_cell_properties / quantify) expects
    # unique integer label ids per cell.
    nuclei_mask = nuclei_2d.astype(np.uint32, copy=False)
    cell_mask = cell_2d.astype(np.uint32, copy=False)

    n_nuclei = int(nuclei_mask.max())
    n_cells = int(cell_mask.max())
    logger.info(f"  Nuclei detected: {n_nuclei}")
    logger.info(f"  Cells detected:  {n_cells}")

    # 5. Save outputs (mirror segment.py naming)
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
    tifffile.imwrite(nuclei_mask_path, nuclei_mask, compression="zlib", bigtiff=True)
    del nuclei_mask

    logger.info(f"  Cell mask: {cell_mask_path.name}")
    tifffile.imwrite(cell_mask_path, cell_mask, compression="zlib", bigtiff=True)
    del cell_mask

    logger.info("")
    logger.info("=" * 80)
    logger.info("✓ INSTANSEG SEGMENTATION COMPLETE")
    logger.info("=" * 80)

    return str(nuclei_mask_path), str(cell_mask_path)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Cell segmentation using InstanSeg on multichannel images",
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
        "--model-name",
        type=str,
        default="fluorescence_nuclei_and_cells",
        help="InstanSeg pretrained model name",
    )

    parser.add_argument(
        "--target",
        type=str,
        default="all_outputs",
        choices=["all_outputs", "cells", "nuclei"],
        help="InstanSeg eval target (which masks to produce)",
    )

    parser.add_argument(
        "--pixel-size",
        type=float,
        default=None,
        help="Override pixel size (µm/px). If omitted, InstanSeg auto-detects from OME metadata.",
    )

    parser.add_argument(
        "--use-gpu",
        action="store_true",
        default=False,
        help="Request GPU (InstanSeg picks the device automatically; informational only)",
    )

    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="Output filename prefix (defaults to input image stem)",
    )

    parser.add_argument(
        "--tile-size",
        type=int,
        default=512,
        help="Sliding-window tile size (px) for eval_medium_image",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=16,
        help="Tiles per forward pass; raise on large GPUs for throughput",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    configure_logging(level=logging.INFO)
    args = parse_args()

    run_instanseg(
        image_path=args.image,
        output_dir=args.output_dir,
        model_name=args.model_name,
        target=args.target,
        pixel_size=args.pixel_size,
        use_gpu=args.use_gpu,
        prefix=args.prefix,
        tile_size=args.tile_size,
        batch_size=args.batch_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
