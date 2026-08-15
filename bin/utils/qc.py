"""Quality control image generation for registration.

This module provides functions for creating QC visualizations to assess
registration quality, particularly RGB composite overlays of registered
and reference images.

Examples
--------
>>> from qc import create_registration_qc
>>> create_registration_qc(
...     reference_path="ref.tif",
...     registered_path="registered.tif",
...     output_path="qc.tif"
... )

Notes
-----
This module consolidates QC generation code that was previously embedded
in registration scripts (register_cpu.py, register_gpu.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import cv2
import numpy as np
import tifffile
from image_io import write_ome_tiff
from logger import get_logger
from metadata import extract_channel_names_from_ome, pick_nuclear_index
from numpy.typing import NDArray
from registration_utils import autoscale
from skimage.transform import rescale

__all__ = [
    "create_registration_qc",
    "create_nuclear_overlay",
    "autoscale_for_display",
]

logger = get_logger(__name__)


def autoscale_for_display(img: NDArray, method: str = "minmax") -> NDArray[np.uint8]:
    """Autoscale image for display purposes.

    Parameters
    ----------
    img : NDArray
        Input image of any numeric dtype.
    method : str, default="minmax"
        Scaling method:
        - 'minmax': Min-max normalization (linear)
        - 'percentile': Percentile-based (robust to outliers)

    Returns
    -------
    NDArray[np.uint8]
        Scaled image in 0-255 range.

    Examples
    --------
    >>> img = np.random.rand(100, 100) * 1000
    >>> scaled = autoscale_for_display(img, method='minmax')
    >>> scaled.min(), scaled.max()
    (0, 255)

    See Also
    --------
    autoscale : Percentile-based scaling from registration_utils
    """
    if method == "minmax":
        img_min = img.min()
        img_max = img.max()
        range_val = max(img_max - img_min, 1e-6)
        normalized = (img - img_min) / range_val
        # Fix: Round before converting to uint8 to avoid truncation artifacts
        return np.round(normalized * 255).astype(np.uint8)
    elif method == "percentile":
        return autoscale(img, low_p=1.0, high_p=99.0)
    else:
        raise ValueError(f"Unknown method: {method}. Use 'minmax' or 'percentile'.")


def create_nuclear_overlay(
    reference_nuc: NDArray, registered_nuc: NDArray, scale_factor: float = 0.25
) -> Tuple[NDArray, NDArray]:
    """Create RGB overlay of the reference and registered nuclear/fiducial channels.

    Parameters
    ----------
    reference_nuc : NDArray
        Reference nuclear/fiducial channel (2D array).
    registered_nuc : NDArray
        Registered nuclear/fiducial channel (2D array).
    scale_factor : float, default=0.25
        Downsampling factor for output images (0.25 = 4x smaller).

    Returns
    -------
    rgb_bgr : NDArray
        RGB composite in BGR order (for OpenCV/PNG), shape (H, W, 3).
        Red = registered, Green = reference, Blue = 0.
    rgb_cyx : NDArray
        RGB composite in CYX order (for TIFF/ImageJ), shape (3, H, W).
        Same channel assignment as rgb_bgr.

    Notes
    -----
    Each channel is independently autoscaled using min-max normalization
    before creating the composite. This ensures both channels are visible
    even if they have different intensity ranges.

    The downsampling uses anti-aliasing to prevent artifacts.

    Examples
    --------
    >>> ref = np.random.randint(0, 4096, (2048, 2048), dtype=np.uint16)
    >>> reg = np.random.randint(0, 4096, (2048, 2048), dtype=np.uint16)
    >>> rgb_bgr, rgb_cyx = create_nuclear_overlay(ref, reg, scale_factor=0.25)
    >>> rgb_bgr.shape
    (512, 512, 3)
    >>> rgb_cyx.shape
    (3, 512, 512)

    See Also
    --------
    create_registration_qc : High-level QC generation function
    autoscale_for_display : Scaling function used internally
    """
    # Validate matching shapes
    if reference_nuc.shape != registered_nuc.shape:
        raise ValueError(
            f"Shape mismatch: reference nuclear channel {reference_nuc.shape} != "
            f"registered nuclear channel {registered_nuc.shape}. "
            f"Images must have the same dimensions after registration."
        )

    # Autoscale each channel independently
    ref_scaled = autoscale_for_display(reference_nuc, method="minmax")
    reg_scaled = autoscale_for_display(registered_nuc, method="minmax")

    # Downsample if requested
    if scale_factor != 1.0:
        # Fix: Round before converting to uint8 to avoid truncation artifacts
        ref_down = np.round(
            rescale(
                ref_scaled, scale=scale_factor, anti_aliasing=True, preserve_range=True
            )
        ).astype(np.uint8)
        reg_down = np.round(
            rescale(
                reg_scaled, scale=scale_factor, anti_aliasing=True, preserve_range=True
            )
        ).astype(np.uint8)
    else:
        ref_down = ref_scaled
        reg_down = reg_scaled

    h, w = reg_down.shape

    # Create RGB composite in BGR order (for OpenCV)
    rgb_bgr = np.zeros((h, w, 3), dtype=np.uint8)
    rgb_bgr[:, :, 2] = reg_down  # Red = registered (BGR so index 2 is Red)
    rgb_bgr[:, :, 1] = ref_down  # Green = reference
    rgb_bgr[:, :, 0] = 0  # Blue = 0

    # Create CYX version for TIFF
    rgb_cyx = np.stack(
        [
            reg_down,  # Red channel (registered)
            ref_down,  # Green channel (reference)
            np.zeros_like(ref_down, dtype=np.uint8),  # Blue channel
        ],
        axis=0,
    )

    return rgb_bgr, rgb_cyx


def create_registration_qc(
    reference_path: str | Path,
    registered_path: str | Path,
    output_path: str | Path,
    scale_factor: float = 0.25,
    save_fullres: bool = True,
    save_png: bool = True,
    save_tiff: bool = True,
    nuclear_markers=None,
) -> None:
    """Create QC visualizations for registration assessment.

    This function generates RGB composite images showing the overlay of the
    reference and registered nuclear/fiducial channels. Perfect registration
    appears as yellow (red + green), while misalignment shows red/green fringing.

    Parameters
    ----------
    reference_path : str or Path
        Path to reference image (OME-TIFF).
    registered_path : str or Path
        Path to registered image (OME-TIFF).
    output_path : str or Path
        Base path for output files (extension will be modified).
    scale_factor : float, default=0.25
        Downsampling factor for preview images (0.25 = 4x smaller).
        Full resolution is always saved if save_fullres=True.
    save_fullres : bool, default=True
        Save full-resolution TIFF (with compression).
    save_png : bool, default=True
        Save downsampled PNG preview.
    save_tiff : bool, default=True
        Save downsampled TIFF (OME, same convention as the full-resolution one).
    nuclear_markers : sequence of str, optional
        Ordered preference list of nuclear/fiducial marker names, i.e.
        ``params.nuclear_markers``. Resolved against each image's OME channel
        names by ``metadata.pick_nuclear_index``, the same rule
        ``lib/MarkerUtils.groovy`` and ``split_multichannel.py`` use. Defaults to
        ``metadata.DEFAULT_NUCLEAR_MARKERS`` so the script stays usable by hand.

    Returns
    -------
    None
        Saves files to disk.

    Notes
    -----
    Output files created:
    - {output_path}_fullres.tif: Full resolution, compressed OME-TIFF (if save_fullres=True)
    - {output_path}.png: Downsampled PNG (if save_png=True)
    - {output_path}.tif: Downsampled OME-TIFF (if save_tiff=True)

    Channel assignment in composites:
    - Red: Registered image
    - Green: Reference image
    - Blue: 0 (black)

    Perfect alignment appears yellow (red + green).
    Misalignment shows red/green fringing.

    Examples
    --------
    >>> create_registration_qc(
    ...     reference_path="reference_DAPI_SMA.tif",
    ...     registered_path="registered_DAPI_CD3.tif",
    ...     output_path="qc_output.tif"
    ... )
    # Creates: qc_output_fullres.tif, qc_output.png, qc_output.tif

    Custom scaling and selective output:
    >>> create_registration_qc(
    ...     reference_path="ref.tif",
    ...     registered_path="reg.tif",
    ...     output_path="qc.tif",
    ...     scale_factor=0.5,
    ...     save_fullres=False,
    ...     save_png=True,
    ...     save_tiff=False
    ... )

    See Also
    --------
    create_nuclear_overlay : Lower-level overlay creation
    """
    reference_path = Path(reference_path)
    registered_path = Path(registered_path)
    output_path = Path(output_path)

    logger.info(f"Creating QC composite: {output_path.name}")

    # Validate input files exist
    if not reference_path.exists():
        raise FileNotFoundError(f"Reference image not found: {reference_path}")
    if not registered_path.exists():
        raise FileNotFoundError(f"Registered image not found: {registered_path}")

    # Load images. Memory-mapped, not decoded whole: exactly one channel of each
    # slide is used below (the nuclear/fiducial channel `pick_nuclear_index`
    # resolves), and GENERATE_REGISTRATION_QC opens the reference and the
    # registered slide together -- so the eager `tifffile.imread` form held two
    # complete multichannel WSIs in RAM at once to read two planes. This is the
    # same read `segment.py` already does (`tif.asarray(out="memmap")`).
    with tifffile.TiffFile(str(reference_path)) as tif:
        ref_img = tif.asarray(out="memmap")
    with tifffile.TiffFile(str(registered_path)) as tif:
        reg_img = tif.asarray(out="memmap")

    # Ensure 3D shape (C, H, W)
    if ref_img.ndim == 2:
        ref_img = ref_img[np.newaxis, ...]
    if reg_img.ndim == 2:
        reg_img = reg_img[np.newaxis, ...]

    # Resolve the nuclear/fiducial channel from OME metadata (convert_image guarantees
    # OME-XML is always present). This MUST go through pick_nuclear_index rather than
    # testing for the literal "DAPI": nuclear_markers is configurable and a CELLTOX-only
    # panel is a supported input. The old literal test worked only by accident -- it
    # fell back to channel 0, which CONVERT_IMAGE happens to reserve for the nuclear
    # channel -- so it produced a correct overlay and a spurious warning on every
    # non-DAPI slide, and would have picked the wrong channel the moment that promotion
    # invariant changed.
    ref_channels = extract_channel_names_from_ome(reference_path)
    reg_channels = extract_channel_names_from_ome(registered_path)

    ref_nuc_idx = pick_nuclear_index(ref_channels, nuclear_markers)
    if ref_nuc_idx is None:
        logger.warning(
            f"No nuclear/fiducial channel found in reference image "
            f"{reference_path.name} (channels: {ref_channels}, markers: "
            f"{list(nuclear_markers) if nuclear_markers else 'default'}). "
            f"Falling back to channel 0."
        )
        ref_nuc_idx = 0

    reg_nuc_idx = pick_nuclear_index(reg_channels, nuclear_markers)
    if reg_nuc_idx is None:
        logger.warning(
            f"No nuclear/fiducial channel found in registered image "
            f"{registered_path.name} (channels: {reg_channels}, markers: "
            f"{list(nuclear_markers) if nuclear_markers else 'default'}). "
            f"Falling back to channel 0."
        )
        reg_nuc_idx = 0

    if ref_channels:
        logger.debug(
            f"Reference nuclear channel: {ref_nuc_idx} ({ref_channels[ref_nuc_idx]})"
        )
    if reg_channels:
        logger.debug(
            f"Registered nuclear channel: {reg_nuc_idx} ({reg_channels[reg_nuc_idx]})"
        )

    ref_nuc = ref_img[ref_nuc_idx]
    reg_nuc = reg_img[reg_nuc_idx]

    # Save full-resolution QC (compressed)
    if save_fullres:
        ref_nuc_scaled = autoscale_for_display(ref_nuc, method="minmax")
        reg_nuc_scaled = autoscale_for_display(reg_nuc, method="minmax")

        rgb_stack_full = np.stack(
            [
                reg_nuc_scaled,  # Red channel (registered)
                ref_nuc_scaled,  # Green channel (reference)
                np.zeros_like(ref_nuc_scaled, dtype=np.uint8),  # Blue channel
            ],
            axis=0,
        )

        fullres_output_path = output_path.with_name(output_path.stem + "_fullres.tif")
        # OME, not ImageJ. This write and the downsampled one below used to sit in
        # this same function under two different metadata conventions -- ImageJ here,
        # OME there -- and, worse, only the *downsampled* one carried bigtiff. This
        # one is the RGB stack of two full-size nuclear channels: the write in this
        # function actually capable of crossing 4 GB. Both are OME now, and
        # write_ome_tiff sets bigtiff on every write it makes.
        write_ome_tiff(
            fullres_output_path,
            rgb_stack_full,
            metadata={"axes": "CYX"},
            compression="zlib",
        )
        logger.info(f"  Saved full-res QC TIFF: {fullres_output_path}")
        del rgb_stack_full

    # Create downsampled overlay
    rgb_bgr, rgb_cyx = create_nuclear_overlay(
        ref_nuc, reg_nuc, scale_factor=scale_factor
    )

    # Save PNG (OpenCV uses BGR order)
    if save_png:
        png_output_path = output_path.with_suffix(".png")
        cv2.imwrite(str(png_output_path), rgb_bgr)
        logger.info(f"  Saved QC PNG: {png_output_path}")

    # Save TIFF (OME, CYX order -- same convention as the full-res composite above).
    # Uncompressed as before: this one is downsampled by `scale_factor`, so it is the
    # small artifact of the pair. The "mode": "composite" key the previous call passed
    # is gone because tifffile's OME writer silently drops it -- it is ImageJ metadata,
    # and carrying it here only made the two writes look like they agreed.
    if save_tiff:
        tiff_output_path = output_path.with_suffix(".tif")
        write_ome_tiff(
            tiff_output_path,
            rgb_cyx,
            metadata={"axes": "CYX"},
            compression=None,
        )
        logger.info(f"  Saved QC TIFF: {tiff_output_path}")

    logger.info("QC generation complete")
