#!/usr/bin/env python3
"""Export cell data to QuPath-compatible GeoJSON.

Generates a QuPath-native GeoJSON FeatureCollection with raw marker intensities
and morphological measurements for all cells. No phenotype classification is
applied — gating is handled downstream by FlowPath in QuPath.

Outputs:
    - cells.geojson: QuPath-compatible cell detections (native array measurement format)
    - cells_data.csv: Full cell data with raw intensities and z-scores per marker
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
from scipy.stats import zscore

sys.path.insert(0, str(Path(__file__).parent / 'utils'))

from image_utils import ensure_dir
from logger import configure_logging, get_logger

logger = get_logger(__name__)

# Columns that are morphological / metadata, not marker intensities
MORPHOLOGY_COLS = {
    'label', 'y', 'x', 'area', 'eccentricity', 'perimeter',
    'convex_area', 'axis_major_length', 'axis_minor_length', 'solidity',
    'fov', 'cell_size',
}

# Default classification color for "Cell" (cyan)
CELL_COLOR_RGB = (0, 255, 255)


def rgb_to_qupath_color(r: int, g: int, b: int, a: int = 255) -> int:
    """Convert RGB to QuPath's signed 32-bit ARGB color format."""
    value = (a << 24) | (r << 16) | (g << 8) | b
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def identify_marker_columns(df: pd.DataFrame) -> List[str]:
    """Identify marker intensity columns (all numeric columns not in morphology set)."""
    markers = []
    for col in df.columns:
        if col in MORPHOLOGY_COLS:
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        markers.append(col)
    return markers


def compute_zscores(df: pd.DataFrame, marker_cols: List[str]) -> pd.DataFrame:
    """Compute z-scores for marker columns and add as new columns.

    Parameters
    ----------
    df : DataFrame
        Cell data with raw marker intensities.
    marker_cols : list of str
        Marker column names to z-score.

    Returns
    -------
    DataFrame
        Input dataframe with added z-score columns (e.g. 'CD45_zscore').
    """
    if not marker_cols:
        return df

    df = df.copy()
    marker_data = df[marker_cols]

    # Compute z-scores (handle constant columns gracefully)
    z_values = pd.DataFrame(
        zscore(marker_data, axis=0, nan_policy='omit'),
        index=marker_data.index,
        columns=marker_data.columns,
    )
    # Replace NaN z-scores (from constant columns) with 0
    z_values = z_values.fillna(0.0)

    for col in marker_cols:
        df[f'{col}_zscore'] = z_values[col]

    return df


def build_measurements(
    row: pd.Series,
    marker_cols: List[str],
    pixel_size: float,
) -> List[Dict]:
    """Build QuPath-native measurement array for a single cell.

    Returns a list of {"name": ..., "value": ...} dicts matching QuPath's
    native GeoJSON serialization format.
    """
    measurements = []

    # Centroid in micrometers
    x_px = float(row.get('x', 0))
    y_px = float(row.get('y', 0))
    measurements.append({"name": "Centroid X µm", "value": round(x_px * pixel_size, 3)})
    measurements.append({"name": "Centroid Y µm", "value": round(y_px * pixel_size, 3)})

    # Raw marker intensities
    for col in marker_cols:
        val = row.get(col)
        if pd.notna(val):
            measurements.append({"name": col, "value": round(float(val), 4)})

    # Morphological features (converted to µm where appropriate)
    area = row.get('area')
    if pd.notna(area):
        measurements.append({"name": "Area µm²", "value": round(float(area) * pixel_size * pixel_size, 3)})

    for morph_col, display_name in [
        ('eccentricity', 'Eccentricity'),
        ('perimeter', 'Perimeter'),
        ('solidity', 'Solidity'),
        ('convex_area', 'Convex Area'),
        ('axis_major_length', 'Major Axis Length'),
        ('axis_minor_length', 'Minor Axis Length'),
    ]:
        val = row.get(morph_col)
        if pd.notna(val):
            measurements.append({"name": display_name, "value": round(float(val), 4)})

    return measurements


def export_geojson(
    df: pd.DataFrame,
    output_path: str,
    pixel_size: float = 0.325,
    marker_cols: Optional[List[str]] = None,
    contours: Optional[Dict[str, List[List[float]]]] = None,
) -> int:
    """Export cells to QuPath-native GeoJSON format.

    Parameters
    ----------
    df : DataFrame
        Cell data with coordinates and marker intensities.
    output_path : str
        Path for output GeoJSON file.
    pixel_size : float
        Micrometers per pixel for coordinate conversion.
    marker_cols : list, optional
        Marker columns to include. If None, auto-detected.
    contours : dict, optional
        Pre-computed contours mapping str(label) -> [[x,y], ...] polygon ring.

    Returns
    -------
    int
        Number of cells exported.
    """
    if marker_cols is None:
        marker_cols = identify_marker_columns(df)

    logger.info(f"Exporting {len(df)} cells to GeoJSON: {output_path}")
    logger.info(f"  Markers: {marker_cols}")

    color_int = rgb_to_qupath_color(*CELL_COLOR_RGB)

    features = []
    skipped = 0

    for idx, row in df.iterrows():
        x_px = row.get('x')
        y_px = row.get('y')
        if pd.isna(x_px) or pd.isna(y_px):
            skipped += 1
            continue

        cell_id = row.get('label', idx)
        cell_label_str = str(int(cell_id)) if pd.notna(cell_id) else str(idx)

        # Geometry: Polygon if contour available, else Point
        if contours and cell_label_str in contours:
            geometry = {
                "type": "Polygon",
                "coordinates": [contours[cell_label_str]],
            }
        else:
            geometry = {
                "type": "Point",
                "coordinates": [float(x_px), float(y_px)],
            }

        # Build measurements array (QuPath native format)
        measurements = build_measurements(row, marker_cols, pixel_size)

        feature = {
            "type": "Feature",
            "id": "PathDetectionObject",
            "geometry": geometry,
            "properties": {
                "objectType": "detection",
                "classification": {
                    "name": "Cell",
                    "colorRGB": color_int,
                },
                "isLocked": False,
                "measurements": measurements,
            },
        }
        features.append(feature)

    logger.info(f"  Exported {len(features)} cells, skipped {skipped}")

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    with open(output_path, 'w') as f:
        json.dump(geojson, f)

    file_size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    logger.info(f"  GeoJSON size: {file_size_mb:.1f} MB")

    return len(features)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Export cell data to QuPath-compatible GeoJSON',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--cell_data', required=True,
        help='Path to merged quantification CSV (from MERGE_QUANT_CSVS)',
    )
    parser.add_argument(
        '-o', '--output_dir', required=True,
        help='Output directory',
    )
    parser.add_argument(
        '--contours_json', type=str, default=None,
        help='Path to pre-computed contours JSON (from extract_cell_properties.py)',
    )
    parser.add_argument(
        '--pixel_size', type=float, default=0.325,
        help='Pixel size in micrometers',
    )
    parser.add_argument(
        '--output_prefix', default='cells',
        help='Prefix for output files',
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    configure_logging(level=logging.INFO)
    args = parse_args()

    ensure_dir(args.output_dir)

    logger.info("=" * 80)
    logger.info("Cell GeoJSON Export (QuPath-native format)")
    logger.info("=" * 80)

    # Load data
    logger.info(f"Loading cell data: {args.cell_data}")
    cell_df = pd.read_csv(args.cell_data)

    # Remove duplicates by label if present
    if 'label' in cell_df.columns:
        cell_df = cell_df.drop_duplicates(subset='label', keep='first')

    logger.info(f"Loaded {len(cell_df)} cells")

    # Identify marker columns
    marker_cols = identify_marker_columns(cell_df)
    logger.info(f"Detected {len(marker_cols)} marker channels: {marker_cols}")

    # Load contours
    contours = None
    if args.contours_json:
        logger.info(f"Loading contours: {args.contours_json}")
        with open(args.contours_json, 'r') as f:
            contours = json.load(f)
        logger.info(f"Loaded contours for {len(contours)} cells")

    # Compute z-scores for CSV output
    cell_df_with_z = compute_zscores(cell_df, marker_cols)

    # Export GeoJSON (raw values + morphology only)
    output_geojson = str(Path(args.output_dir) / f'{args.output_prefix}.geojson')
    num_exported = export_geojson(
        df=cell_df,
        output_path=output_geojson,
        pixel_size=args.pixel_size,
        marker_cols=marker_cols,
        contours=contours,
    )

    # Save CSV with both raw and z-score columns
    output_csv = str(Path(args.output_dir) / f'{args.output_prefix}_data.csv')
    logger.info(f"Saving CSV: {output_csv}")
    csv_df = cell_df_with_z.copy()
    csv_df.to_csv(output_csv, index=False)

    # Summary
    logger.info("=" * 80)
    logger.info("EXPORT COMPLETED SUCCESSFULLY")
    logger.info("=" * 80)
    logger.info(f"Output files:")
    logger.info(f"  GeoJSON: {output_geojson} ({num_exported} cells)")
    logger.info(f"  CSV:     {output_csv} ({len(csv_df)} cells, {len(csv_df.columns)} columns)")
    logger.info("")
    logger.info("To view in QuPath:")
    logger.info("  1. Open your pyramidal OME-TIFF image")
    logger.info("  2. Run import_geojson.groovy or use File > Import objects")
    logger.info("  3. Open FlowPath for interactive gating")
    logger.info("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
