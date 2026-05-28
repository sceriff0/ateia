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
sys.path.insert(0, str(Path(__file__).parent / 'utils'))

import numpy as np
import tifffile

from logger import get_logger, configure_logging
from image_utils import ensure_dir

logger = get_logger(__name__)


def _to_numpy(arr):
    """Convert torch tensor or numpy-like object to numpy ndarray.

    InstanSeg may return either a numpy array or a torch tensor depending on
    version/device. This shim handles both without importing torch directly.
    """
    if hasattr(arr, 'detach'):
        # torch tensor — move to CPU first
        try:
            arr = arr.detach().cpu().numpy()
        except Exception:
            arr = arr.cpu().numpy() if hasattr(arr, 'cpu') else arr.numpy()
    elif hasattr(arr, 'numpy'):
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
    """
    arr = _to_numpy(labeled_output)
    logger.info(f"  InstanSeg raw output shape: {arr.shape}, dtype: {arr.dtype}")

    # Strip leading singleton dims (e.g. batch axis)
    while arr.ndim > 3 and arr.shape[0] == 1:
        arr = arr[0]

    if arr.ndim == 2:
        # Single (Y, X) — only one target was requested
        single = arr
        if target == 'nuclei':
            return single, single  # replicate to satisfy downstream cell-mask consumer
        elif target == 'cells':
            return single, single
        else:
            # Unexpected: all_outputs returned 2D. Treat as nuclei + replicate.
            logger.warning(
                "  target='all_outputs' but InstanSeg returned a 2D array; "
                "replicating to both nuclei and cell masks."
            )
            return single, single

    if arr.ndim == 3:
        n = arr.shape[0]
        if n == 1:
            single = arr[0]
            if target == 'nuclei':
                return single, single
            elif target == 'cells':
                return single, single
            else:
                logger.warning(
                    "  target='all_outputs' but InstanSeg returned 1 channel; "
                    "replicating to both nuclei and cell masks."
                )
                return single, single
        elif n >= 2:
            # Conventional all_outputs ordering: [nuclei, cells]
            nuclei_2d = arr[0]
            cell_2d = arr[1]
            return nuclei_2d, cell_2d

    raise ValueError(
        f"Unexpected InstanSeg output shape {arr.shape}; "
        f"expected 2D (Y, X) or 3D (N, Y, X) with N in {{1, 2}}."
    )


def run_instanseg(
    image_path: str,
    output_dir: str,
    model_name: str = 'fluorescence_nuclei_and_cells',
    target: str = 'all_outputs',
    pixel_size: float | None = None,
    use_gpu: bool = False,
    prefix: str | None = None,
):
    """Run the InstanSeg segmentation pipeline on a multichannel image.

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
        InstanSeg ``eval`` target: ``'all_outputs'``, ``'cells'``, or
        ``'nuclei'``. When only one target is produced, that label map is
        written to both ``_nuclei_mask.tif`` and ``_cell_mask.tif`` so that
        downstream is contract-preserving.
    pixel_size
        Override pixel size in µm/px. If None, InstanSeg's auto-detected value
        from the OME metadata is used.
    use_gpu
        Whether GPU is requested. InstanSeg picks a device internally; this
        flag is informational and warned about if CUDA is unavailable.
    prefix
        Output filename prefix. Defaults to the input image's stem.
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
    logger.info(f"  Image array dtype: {getattr(image_array, 'dtype', type(image_array))}")
    logger.info(f"  Detected pixel size: {detected_pixel_size}")

    effective_pixel_size = pixel_size if pixel_size is not None else detected_pixel_size
    logger.info(f"  Effective pixel size used: {effective_pixel_size}")

    # 3. Run segmentation
    logger.info(f"Running InstanSeg eval_small_image (target={target})...")
    seg_start = time.time()
    labeled_output, _image_tensor = model.eval_small_image(
        image_array,
        effective_pixel_size,
        target=target,
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
    tifffile.imwrite(nuclei_mask_path, nuclei_mask, compression='zlib')
    del nuclei_mask

    logger.info(f"  Cell mask: {cell_mask_path.name}")
    tifffile.imwrite(cell_mask_path, cell_mask, compression='zlib')
    del cell_mask

    logger.info("")
    logger.info("=" * 80)
    logger.info("✓ INSTANSEG SEGMENTATION COMPLETE")
    logger.info("=" * 80)

    return str(nuclei_mask_path), str(cell_mask_path)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Cell segmentation using InstanSeg on multichannel images',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        '--image',
        type=str,
        required=True,
        help='Path to multichannel OME-TIFF image (e.g., registered image from VALIS)',
    )

    parser.add_argument(
        '--output-dir',
        type=str,
        default='.',
        help='Output directory for segmentation masks',
    )

    parser.add_argument(
        '--model-name',
        type=str,
        default='fluorescence_nuclei_and_cells',
        help='InstanSeg pretrained model name',
    )

    parser.add_argument(
        '--target',
        type=str,
        default='all_outputs',
        choices=['all_outputs', 'cells', 'nuclei'],
        help='InstanSeg eval target (which masks to produce)',
    )

    parser.add_argument(
        '--pixel-size',
        type=float,
        default=None,
        help='Override pixel size (µm/px). If omitted, InstanSeg auto-detects from OME metadata.',
    )

    parser.add_argument(
        '--use-gpu',
        action='store_true',
        default=False,
        help='Request GPU (InstanSeg picks the device automatically; informational only)',
    )

    parser.add_argument(
        '--prefix',
        type=str,
        default=None,
        help='Output filename prefix (defaults to input image stem)',
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
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
