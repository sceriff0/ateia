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
``create_registration_qc`` reads its nuclear/fiducial channel through
``tiled_io.open_lazy`` + ``read_decimated`` rather than ``tifffile.imread``ing
every marker channel of the whole slide. What that removes is the
**N-channel overread**: only the ``pick_nuclear_index``-resolved channel is
ever decoded, not every marker channel in the panel. It does **not** remove
any spatial overread -- the read stays at full resolution
(``decimation_factor(..., max_dim=None)`` always returns ``1`` here), because
``GENERATE_REGISTRATION_QC`` runs with ``save_fullres=True`` unconditionally
in production (see ``conf/modules.config``'s ``GENERATE_REGISTRATION_QC``
block: no ``--no-fullres`` is ever passed), so the ``*_fullres.tif`` artifact
this function publishes genuinely needs full-resolution pixels. Decimating
the read would change that published artifact, not just the (already
downsampled) preview.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import cv2
import numpy as np
from logger import get_logger
from metadata import extract_channel_names_from_ome, pick_nuclear_index
from numpy.typing import NDArray
from ome_io import write_tiff
from registration_utils import autoscale
from skimage.transform import rescale
from tiled_io import decimation_factor, open_lazy, read_decimated

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
        Save downsampled TIFF for ImageJ.
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
    - {output_path}_fullres.tif: Full resolution, compressed (if save_fullres=True)
    - {output_path}.png: Downsampled PNG (if save_png=True)
    - {output_path}.tif: Downsampled ImageJ TIFF (if save_tiff=True)

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

    # Resolve the nuclear/fiducial channel from OME metadata (convert_image guarantees
    # OME-XML is always present) BEFORE touching pixel data, so a malformed/missing
    # OME-XML fails fast without paying for opening either slide's pixel store. This MUST
    # go through pick_nuclear_index rather than testing for the literal "DAPI":
    # nuclear_markers is configurable and a CELLTOX-only panel is a supported input. The
    # old literal test worked only by accident -- it fell back to channel 0, which
    # CONVERT_IMAGE happens to reserve for the nuclear channel -- so it produced a correct
    # overlay and a spurious warning on every non-DAPI slide, and would have picked the
    # wrong channel the moment that promotion invariant changed.
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

    # Read only the resolved nuclear/fiducial channel of each slide, through a lazy,
    # region-readable zarr view (open_lazy) rather than tifffile.imread's whole-stack
    # read of every marker channel at full resolution -- the N-CHANNEL overread this step
    # used to pay, twice, on the two largest inputs the QC step sees. This is NOT a
    # spatial decimation: `factor` is 1 here, and stays 1, because this function always
    # needs true full-resolution pixels (the *_fullres.tif output is unconditional in
    # production -- see the module docstring). Only the unused channels are no longer
    # read; every pixel of the used one still is. decimation_factor is still the source of
    # true decimation.
    #
    # read_decimated always returns float32 here -- do NOT pass a narrower dtype
    # (uint8/uint16) even though the only consumer of ref_nuc/reg_nuc is
    # autoscale_for_display on the way to uint8 (below, and inside create_nuclear_overlay).
    # This was tried (task 3, first pass) and reverted. Two things are both true and worth
    # keeping straight:
    #   1. Narrowing the read is NOT safe on its own: a uint16 array divided by a uint16
    #      range_val promotes to float64 under numpy's true-divide, where the float32
    #      arithmetic autoscale_for_display has always run stays float32 -- different
    #      intermediate precision that demonstrably rounds np.round(normalized * 255)
    #      differently for real (min, max, value) triples (e.g. min=13286, max=62449,
    #      value=52520: uint16-input -> 203, float32-input -> 204 -- float32's coarser
    #      spacing manufactures a spurious .5 tie that round-half-to-even then resolves the
    #      other way). See
    #      tests/test_tiled_io.py::test_autoscale_uint16_vs_float32_disagree_at_a_real_triple
    #      and tests/test_registration_qc_lazy_read.py's adversarial-pixel test for the
    #      reproduced counter-example. Fixing that requires widening back to float32 right
    #      before autoscale_for_display sees the array.
    #   2. That widen-back is exactly what erases the memory saving the narrow read
    #      appeared to offer. Measured (docs/perf/2026-08-26-rss.md, isolated processes,
    #      7000x6000 fixture): a narrow read that is NEVER widened back does measure ~48%
    #      lower peak RSS than this always-float32 read -- but once the mandatory widen-back
    #      cast runs, the measured peak RSS is statistically indistinguishable from never
    #      narrowing at all (670.8 MB either way). So narrowing here buys nothing once it is
    #      made correct, and only adds a cast plus branching. Left at float32, unconditionally.
    #
    # The real, measured win at this call site is read_decimated's pre-allocated
    # destination (no band-list-plus-concatenate doubling, at any factor): measured ~24%
    # lower peak RSS than the prior list+concatenate implementation, at this exact
    # (default-dtype, no narrowing) call shape -- but that number is a READ-PHASE-ONLY
    # figure (isolated open_lazy + read_decimated, no render). The measured full
    # create_registration_qc peak (read + autoscale/stack/rescale/PNG-TIFF write) showed no
    # equivalent improvement -- statistically within noise of the pre-branch baseline. See
    # docs/perf/2026-08-26-rss.md for both sets of numbers; do not extend the read-phase
    # figure into an end-to-end claim it doesn't support.
    ref_close = reg_close = None
    try:
        ref_arr, _ref_dtype, ref_close = open_lazy(reference_path)
        reg_arr, _reg_dtype, reg_close = open_lazy(registered_path)
        factor = decimation_factor([ref_arr.shape[1:], reg_arr.shape[1:]], max_dim=None)
        ref_nuc = read_decimated(ref_arr, ref_nuc_idx, factor)
        reg_nuc = read_decimated(reg_arr, reg_nuc_idx, factor)
    finally:
        # Both opens now live inside this try, so a failure on the SECOND open_lazy call no
        # longer leaks the first store's handle -- but it also means ref_close may never get
        # bound (open_lazy(reference_path) itself can raise before returning). Guard each
        # close individually rather than assuming both succeeded.
        if ref_close is not None:
            ref_close()
        if reg_close is not None:
            reg_close()

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
        write_tiff(
            str(fullres_output_path),
            rgb_stack_full,
            imagej=True,
            metadata={"axes": "CYX", "mode": "composite"},
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

    # Save TIFF (ImageJ-compatible, CYX order)
    if save_tiff:
        tiff_output_path = output_path.with_suffix(".tif")
        write_tiff(
            str(tiff_output_path),
            rgb_cyx,
            ome=True,
            bigtiff=True,
            metadata={"axes": "CYX", "mode": "composite"},
        )
        logger.info(f"  Saved QC TIFF: {tiff_output_path}")

    logger.info("QC generation complete")
