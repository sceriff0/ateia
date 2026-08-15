"""Image processing utilities for the MIRAGE pipeline.

This module provides common image processing functions used across
multiple scripts, following DRY principles.

Examples
--------
>>> from image_utils import normalize_image_dimensions
>>> img = np.random.rand(100, 100, 3)
>>> normalized = normalize_image_dimensions(img)
>>> normalized.shape
(3, 100, 100)

Notes
-----
This module consolidates image processing code that was previously
duplicated across multiple bin/ scripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import tifffile
from numpy.typing import NDArray

__all__ = [
    "normalize_image_dimensions",
    "load_image",
    "ensure_dir",
]

# There is deliberately no save_tiff here. TIFF *writes* have exactly one owner,
# bin/utils/image_io.py, which sets bigtiff on every write it makes; this module's
# old save_tiff() took bigtiff as a caller-overridable keyword (so the decision was
# not the writer's) and had no importer anywhere in bin/. See image_io's module
# docstring for the four write variants and which script uses which.


def normalize_image_dimensions(img: NDArray) -> NDArray:
    """Normalize image to (C, H, W) format.

    This function handles the common case of images that may be in
    different formats:
    - 2D (H, W) -> (1, H, W)
    - 3D (H, W, C) -> (C, H, W)
    - 3D (C, H, W) -> (C, H, W) [no change]

    Parameters
    ----------
    img : NDArray
        Input image array.
        Can be 2D (grayscale) or 3D (multichannel).

    Returns
    -------
    NDArray
        Normalized array in (C, H, W) format where C is the number of channels.

    Raises
    ------
    ValueError
        If image has incorrect number of dimensions (not 2D or 3D).

    Notes
    -----
    The function detects (H, W, C) vs (C, H, W) format by comparing dimensions:
    - If shape[0] is smallest, assumes (C, H, W) format - no change
    - If shape[2] is smallest, assumes (H, W, C) format - transposes to (C, H, W)

    This heuristic works well for typical microscopy images where:
    - Number of channels (C) is typically 1-10
    - Height and Width are typically 512-4096+ pixels

    Examples
    --------
    Grayscale image:
    >>> img = np.random.rand(1024, 1024)
    >>> result = normalize_image_dimensions(img)
    >>> result.shape
    (1, 1024, 1024)

    RGB image in (H, W, C) format:
    >>> img = np.random.rand(1024, 1024, 3)
    >>> result = normalize_image_dimensions(img)
    >>> result.shape
    (3, 1024, 1024)

    Already normalized:
    >>> img = np.random.rand(3, 1024, 1024)
    >>> result = normalize_image_dimensions(img)
    >>> result.shape
    (3, 1024, 1024)

    See Also
    --------
    numpy.transpose : Transpose array dimensions
    """
    if img.ndim == 2:
        # Grayscale (H, W) -> (1, H, W)
        return img[np.newaxis, ...]

    elif img.ndim == 3:
        # Detect format by comparing dimensions
        # Assumption: channels is the smallest dimension
        if img.shape[0] < img.shape[1] and img.shape[0] < img.shape[2]:
            # Already (C, H, W) format
            return img
        elif img.shape[2] < img.shape[0] and img.shape[2] < img.shape[1]:
            # (H, W, C) format - transpose to (C, H, W)
            return np.transpose(img, (2, 0, 1))
        else:
            # Ambiguous - assume (C, H, W) and keep as-is
            # This handles square images or unusual dimensions
            return img

    else:
        raise ValueError(
            f"Image must be 2D or 3D, got {img.ndim}D with shape {img.shape}"
        )


def ensure_dir(directory: str | Path) -> Path:
    """Ensure directory exists, creating it if necessary.

    Parameters
    ----------
    directory : str or Path
        Path to directory to create.

    Returns
    -------
    Path
        Path object for the created/existing directory.

    Notes
    -----
    Creates parent directories as needed (parents=True) and does not
    raise an error if directory exists (exist_ok=True).

    Examples
    --------
    >>> output_dir = ensure_dir("results/analysis")
    >>> print(output_dir)
    PosixPath('results/analysis')

    See Also
    --------
    pathlib.Path.mkdir : Underlying method used
    """
    dir_path = Path(directory)
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path


def load_image(
    image_path: str | Path, memmap: bool = False
) -> Tuple[NDArray, Dict[str, Any]]:
    """Load image from TIFF file with metadata extraction.

    Parameters
    ----------
    image_path : str or Path
        Path to TIFF image file.
    memmap : bool, default=False
        If True, use memory-mapped I/O to avoid loading entire file into RAM.

    Returns
    -------
    image : NDArray
        Image data as numpy array. Shape depends on image dimensionality.
    metadata : dict
        Dictionary containing image metadata with keys:
        - 'shape': tuple - Image shape
        - 'dtype': np.dtype - Data type
        - 'ome': str or None - OME-XML metadata if available
        - 'axes': str or None - Dimension order (e.g., 'CYX')

    Raises
    ------
    FileNotFoundError
        If image_path does not exist.
    ValueError
        If file cannot be read as TIFF.

    Notes
    -----
    This function handles both standard TIFF and OME-TIFF formats.
    Memory-mapped mode is recommended for images >1GB.

    Examples
    --------
    >>> img, meta = load_image("sample.tif")
    >>> print(img.shape, img.dtype)
    (3, 2048, 2048) uint16

    >>> # Memory-mapped for large files
    >>> img, meta = load_image("large.tif", memmap=True)

    See Also
    --------
    image_io : the one owner of TIFF writes (write_ome_tiff, write_mask_tiff,
        write_plain_tiff, open_tiff_writer)
    """
    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    try:
        with tifffile.TiffFile(str(image_path)) as tif:
            if memmap:
                image = tif.asarray(out="memmap")
            else:
                image = tif.asarray()

            metadata = {
                "shape": image.shape,
                "dtype": image.dtype,
                "ome": None,
                "axes": None,
            }

            if hasattr(tif, "ome_metadata") and tif.ome_metadata:
                metadata["ome"] = tif.ome_metadata

            return image, metadata

    except Exception as e:
        raise ValueError(f"Failed to load image {image_path}: {e}") from e
