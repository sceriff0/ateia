#!/usr/bin/env python3
"""Extract cell morphology and contours from a segmentation mask.

Computes regionprops (morphology) and polygon contours once per mask,
so downstream processes (QUANTIFY, EXPORT_GEOJSON) don't need to recompute them.

Outputs:
    - morphology.csv: Per-cell morphological properties
    - contours.json: Per-cell simplified polygon contours for GeoJSON export
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from skimage.measure import (
    approximate_polygon,
    find_contours,
    regionprops_table,
)

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / 'utils'))

from image_utils import ensure_dir, load_image
from logger import configure_logging, get_logger

logger = get_logger(__name__)


def extract_morphology(
    mask: NDArray,
    min_area: int = 0,
) -> Tuple[Optional[pd.DataFrame], Optional[NDArray], Optional[NDArray]]:
    """Compute morphological properties for all cells above min_area.

    Parameters
    ----------
    mask : ndarray, shape (Y, X)
        Segmentation mask with cell labels (background=0).
    min_area : int
        Minimum cell area in pixels.

    Returns
    -------
    props_df : DataFrame or None
        Morphological properties with columns:
        label, y, x, area, eccentricity, perimeter, convex_area,
        axis_major_length, axis_minor_length, plus bbox columns
        (bbox-0..bbox-3) used internally by extract_contours.
    mask_filtered : ndarray or None
        Mask with only valid cells (small cells zeroed out).
    valid_labels : ndarray or None
        Array of valid label IDs.
    """
    mask = np.ascontiguousarray(mask.squeeze())

    labels, counts = np.unique(mask, return_counts=True)
    valid_labels = labels[(labels != 0) & (counts >= min_area)]

    if len(valid_labels) == 0:
        logger.warning("No valid cells found after area filtering")
        return None, None, None

    mask_filtered = np.where(np.isin(mask, valid_labels), mask, 0)

    if np.all(mask_filtered == 0):
        logger.warning("Filtered mask is empty")
        return None, None, None

    logger.info(f"Computing morphology for {len(valid_labels)} cells...")
    props = regionprops_table(
        mask_filtered,
        properties=[
            'label', 'centroid', 'area',
            'eccentricity', 'perimeter',
            'convex_area', 'axis_major_length', 'axis_minor_length',
            'solidity', 'bbox',
        ],
    )
    props_df = pd.DataFrame(props).set_index('label')
    props_df.rename(
        columns={'centroid-0': 'y', 'centroid-1': 'x'},
        inplace=True,
    )

    logger.info(f"Morphology computed for {len(props_df)} cells")
    return props_df, mask_filtered, valid_labels


def extract_contours(
    mask_filtered: NDArray,
    props_df: pd.DataFrame,
    simplify_tolerance: float = 0.5,
) -> Dict[str, List[List[float]]]:
    """Extract simplified polygon contours for each cell.

    Uses find_contours on each cell's bounding box crop for efficiency,
    then simplifies with Ramer-Douglas-Peucker via approximate_polygon.

    Parameters
    ----------
    mask_filtered : ndarray, shape (Y, X)
        Filtered segmentation mask.
    props_df : DataFrame
        Morphology DataFrame indexed by label, must contain bbox columns
        (bbox-0, bbox-1, bbox-2, bbox-3) from regionprops_table.
    simplify_tolerance : float
        Douglas-Peucker simplification tolerance in pixels.

    Returns
    -------
    contours : dict
        Mapping of str(label) -> [[x, y], ...] closed polygon ring
        in pixel coordinates.
    """
    logger.info(f"Extracting contours for {len(props_df)} cells (tolerance={simplify_tolerance})...")

    contours_dict: Dict[str, List[List[float]]] = {}
    skipped_no_contour = 0
    skipped_too_simple = 0

    for label, row in props_df.iterrows():
        minr, minc, maxr, maxc = int(row['bbox-0']), int(row['bbox-1']), int(row['bbox-2']), int(row['bbox-3'])

        # Crop binary mask for this cell, padded by 1px to ensure closed contours
        # for cells touching the image border (find_contours returns open contours
        # at array edges, which would produce incorrect polygon geometry)
        binary_crop = np.pad(
            (mask_filtered[minr:maxr, minc:maxc] == label).astype(np.uint8),
            pad_width=1, mode='constant', constant_values=0
        )
        contour_list = find_contours(binary_crop, level=0.5)

        if not contour_list:
            skipped_no_contour += 1
            continue

        # Take the longest contour (outer boundary)
        contour = max(contour_list, key=len)

        # Offset back to full image coordinates (subtract 1 for padding, add bbox origin)
        contour[:, 0] += minr - 1  # y offset
        contour[:, 1] += minc - 1  # x offset

        # Convert from skimage center-of-pixel to ImageJ/QuPath corner-of-pixel convention
        contour += 0.5

        # Simplify with Douglas-Peucker
        simplified = approximate_polygon(contour, tolerance=simplify_tolerance)

        # Need at least 4 vertices for a valid polygon (3 unique + closing point)
        if len(simplified) < 4:
            skipped_too_simple += 1
            continue

        # Close the ring (first == last) for GeoJSON compliance
        if not np.array_equal(simplified[0], simplified[-1]):
            simplified = np.vstack([simplified, simplified[0:1]])

        # Convert from (y, x) to (x, y) for GeoJSON and round for compact JSON
        contours_dict[str(label)] = [
            [round(float(pt[1]), 4), round(float(pt[0]), 4)]
            for pt in simplified
        ]

    skipped = skipped_no_contour + skipped_too_simple
    logger.info(f"Extracted {len(contours_dict)} contours, skipped {skipped} "
                f"({skipped_no_contour} no contour, {skipped_too_simple} too few vertices)")
    return contours_dict


def run_extraction(
    mask_path: str,
    outdir: str,
    min_area: int = 0,
    simplify_tolerance: float = 0.5,
) -> Tuple[Optional[pd.DataFrame], Optional[Dict]]:
    """Run full extraction pipeline: morphology + contours.

    Parameters
    ----------
    mask_path : str
        Path to segmentation mask (.tif or .npy).
    outdir : str
        Output directory.
    min_area : int
        Minimum cell area filter.
    simplify_tolerance : float
        Contour simplification tolerance.

    Returns
    -------
    morphology_df : DataFrame or None
    contours : dict or None
    """
    logger.info("=" * 60)
    logger.info("Extracting cell properties")
    logger.info("=" * 60)

    # Load mask
    logger.info(f"Loading mask: {mask_path}")
    if mask_path.endswith('.npy'):
        mask = np.load(mask_path).squeeze()
    else:
        mask, _ = load_image(mask_path)
        mask = mask.squeeze()
    logger.info(f"Mask shape: {mask.shape}, dtype: {mask.dtype}")

    # Extract morphology
    props_df, mask_filtered, valid_labels = extract_morphology(mask, min_area)

    if props_df is None:
        logger.warning("No valid cells found after min_area filtering — writing empty outputs")
        empty_df = pd.DataFrame(columns=[
            'label', 'y', 'x', 'area', 'eccentricity', 'perimeter',
            'convex_area', 'axis_major_length', 'axis_minor_length', 'solidity',
        ])
        morphology_path = str(Path(outdir) / 'morphology.csv')
        contours_path = str(Path(outdir) / 'contours.json')
        empty_df.to_csv(morphology_path, index=False)
        with open(contours_path, 'w') as f:
            json.dump({}, f)
        return None, None

    # Extract contours (reuse bbox from morphology — no second regionprops call)
    contours = extract_contours(mask_filtered, props_df, simplify_tolerance)

    # Save morphology CSV
    morphology_path = str(Path(outdir) / 'morphology.csv')
    morphology_out = props_df.reset_index()
    morpho_cols = [
        'label', 'y', 'x', 'area', 'eccentricity', 'perimeter',
        'convex_area', 'axis_major_length', 'axis_minor_length', 'solidity',
    ]
    morphology_out = morphology_out[morpho_cols]
    morphology_out.to_csv(morphology_path, index=False)
    logger.info(f"Saved morphology: {morphology_path} ({len(morphology_out)} cells)")

    # Save contours JSON
    contours_path = str(Path(outdir) / 'contours.json')
    with open(contours_path, 'w') as f:
        json.dump(contours, f)
    contours_size_mb = Path(contours_path).stat().st_size / (1024 * 1024)
    logger.info(f"Saved contours: {contours_path} ({len(contours)} cells, {contours_size_mb:.1f} MB)")

    logger.info("Extraction complete")
    return morphology_out, contours


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Extract cell morphology and contours from segmentation mask',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--mask_file', type=str, required=True,
        help='Path to segmentation mask (.tif or .npy)',
    )
    parser.add_argument(
        '--outdir', type=str, default='.',
        help='Output directory',
    )
    parser.add_argument(
        '--min_area', type=int, default=0,
        help='Minimum cell area in pixels',
    )
    parser.add_argument(
        '--simplify_tolerance', type=float, default=0.5,
        help='Douglas-Peucker contour simplification tolerance in pixels',
    )
    return parser.parse_args()


def main():
    """Main entry point."""
    configure_logging(level=logging.INFO)
    args = parse_args()

    ensure_dir(args.outdir)

    run_extraction(
        mask_path=args.mask_file,
        outdir=args.outdir,
        min_area=args.min_area,
        simplify_tolerance=args.simplify_tolerance,
    )

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
