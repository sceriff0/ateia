"""Non-overlapping FOV tiling: the grid arithmetic BASICPY's inputs are built from.

These three functions used to live in ``bin/preprocess.py``, next to the in-process
``BaSiC.fit_transform`` call they were written for. That path is gone -- illumination
correction now runs through nf-core's BASICPY module (``TILE_FOR_BASIC`` -> ``BASICPY``
-> ``APPLY_PROFILES``) -- but the tiling itself is unchanged and is still what makes a
stitched mirage slide look like the multi-site acquisition BaSiC needs to fit a profile.

They live under ``bin/utils/`` now because both halves of that three-process path need
them and neither owns the other:

* ``bin/tile_for_basic.py`` calls ``count_fovs`` + ``split_image_into_fovs`` to write
  the pseudo-FOV stack BASICPY fits on, and records the resulting positions in its
  sidecar;
* ``bin/apply_basic_profiles.py`` calls ``split_image_into_fovs`` +
  ``reconstruct_image_from_fovs`` to apply the returned profiles per tile and reassemble
  the slide.

The split is EXACT and non-overlapping: remainder pixels are distributed across tiles
(some get +1 pixel) rather than padded, so ``reconstruct_image_from_fovs`` applied to
``split_image_into_fovs``' output is the identity. ``count_fovs`` is the matching grid
count, ``ceil(size / fov)`` on each axis.
"""

from __future__ import annotations

import math
from typing import List, Tuple

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "count_fovs",
    "split_image_into_fovs",
    "reconstruct_image_from_fovs",
]


def count_fovs(
    image_shape: Tuple[int, int], fov_size: Tuple[int, int]
) -> Tuple[int, int]:
    """Calculate how many FOVs are needed to cover an image with a given FOV size.

    Tiling is non-overlapping, matching split_image_into_fovs, which extracts
    adjacent tiles and distributes the remainder pixels across them.
    """
    height, width = image_shape[:2]
    fov_h, fov_w = fov_size

    if fov_h <= 0 or fov_w <= 0:
        raise ValueError(f"FOV size must be positive, got {fov_size}")

    n_fovs_y = math.ceil(height / fov_h)
    n_fovs_x = math.ceil(width / fov_w)

    return n_fovs_y, n_fovs_x


def split_image_into_fovs(
    image: NDArray, n_fovs_x: int, n_fovs_y: int
) -> Tuple[NDArray, List[Tuple[int, int, int, int]], Tuple[int, int]]:
    """
    Split image (H, W) into FOV tiles with adaptive sizing to handle remainders.

    The image is divided into n_fovs_y * n_fovs_x tiles. Remainder pixels are
    distributed across tiles (some tiles get +1 pixel) to exactly cover the image
    without padding.
    """
    if image.ndim != 2:
        raise ValueError(f"Image must be 2D, got shape {image.shape}")

    if n_fovs_x <= 0 or n_fovs_y <= 0:
        raise ValueError("Number of FOVs must be positive")

    height, width = image.shape

    # Calculate base FOV sizes and remainders
    base_w = width // n_fovs_x
    base_h = height // n_fovs_y
    remainder_x = width % n_fovs_x
    remainder_y = height % n_fovs_y

    # Calculate actual FOV sizes (some FOVs get +1 pixel to handle remainders)
    fov_widths = [base_w + (1 if j < remainder_x else 0) for j in range(n_fovs_x)]
    fov_heights = [base_h + (1 if i < remainder_y else 0) for i in range(n_fovs_y)]

    max_w = max(fov_widths)
    max_h = max(fov_heights)

    # Create FOV stack with padding to max dimensions
    n_fovs = n_fovs_y * n_fovs_x
    fov_stack = np.zeros((n_fovs, max_h, max_w), dtype=image.dtype)

    # Extract FOVs and store position info
    positions = []
    y_start = 0
    idx = 0

    for i in range(n_fovs_y):
        x_start = 0
        for j in range(n_fovs_x):
            h = fov_heights[i]
            w = fov_widths[j]

            fov_stack[idx, :h, :w] = image[y_start : y_start + h, x_start : x_start + w]

            positions.append((y_start, x_start, h, w))
            x_start += w
            idx += 1
        y_start += fov_heights[i]

    return fov_stack, positions, (max_h, max_w)


def reconstruct_image_from_fovs(
    fov_stack: NDArray,
    positions: List[Tuple[int, int, int, int]],
    original_shape: Tuple[int, ...],
) -> NDArray:
    """
    Reconstruct 2D image from 3D FOV tiles stack.
    """
    reconstructed = np.zeros(original_shape, dtype=fov_stack.dtype)

    for idx, (row_start, col_start, h, w) in enumerate(positions):
        # fov_stack is 3D (N, fov_h, fov_w), reconstructed is 2D (H, W)
        reconstructed[row_start : row_start + h, col_start : col_start + w] = fov_stack[
            idx, :h, :w
        ]

    return reconstructed
