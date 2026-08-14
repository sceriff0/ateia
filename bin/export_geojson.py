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
from typing import Dict, List, Optional

import pandas as pd
from scipy.stats import zscore

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from image_utils import ensure_dir
from logger import configure_logging, get_logger
from measurements import MORPHOLOGY_COLS, identify_marker_columns
from pixel_convention import centre_to_corner

logger = get_logger(__name__)

# Columns that are morphological / metadata, not marker intensities
# (single source of truth: bin/utils/measurements.py). Wrapped in a set here
# since this module does membership tests in a loop.
MORPHOLOGY_COLS = set(MORPHOLOGY_COLS)

# Default classification color for "Cell" (cyan)
CELL_COLOR_RGB = (0, 255, 255)


def rgb_to_qupath_color(r: int, g: int, b: int, a: int = 255) -> int:
    """Convert RGB to QuPath's signed 32-bit ARGB color format."""
    value = (a << 24) | (r << 16) | (g << 8) | b
    if value >= 0x80000000:
        value -= 0x100000000
    return value


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
        zscore(marker_data, axis=0, nan_policy="omit"),
        index=marker_data.index,
        columns=marker_data.columns,
    )
    # For constant columns (std=0), zscore produces NaN for all rows — replace with 0.
    # But preserve NaN for cells that had missing raw data (those should stay NaN).
    for col in marker_cols:
        is_missing_raw = marker_data[col].isna()
        col_std = marker_data[col].std()
        is_constant_col = col_std == 0 or pd.isna(col_std)
        if is_constant_col:
            z_values.loc[~is_missing_raw, col] = 0.0

    for col in marker_cols:
        df[f"{col}_zscore"] = z_values[col]

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

    # Segmentation identity, emitted first. `label` ties this cell to its contour (the
    # lookup export_geojson() does ten lines below) and to every row of cells_data.csv,
    # and it was the one column the CSV kept and the GeoJSON dropped: MORPHOLOGY_COLS
    # excludes it from marker_cols, so nothing else in this function emits it.
    #
    # Joining CSV row i to feature i cannot stand in for it. A cell with a NaN centroid
    # stays in the CSV and never becomes a feature (the `skipped` counter in
    # export_geojson()), so the two orderings diverge the moment one row is skipped --
    # silently, and not detectably from the published artefacts alone.
    #
    # Safe on the QuPath side: FlowPath's MORPHOLOGY_PREFIXES filters "label" out of the
    # marker panel, so it does not appear as a gateable channel.
    label = row.get("label")
    if pd.notna(label):
        measurements.append({"name": "label", "value": int(label)})

    # Centroid in micrometres. The quant CSV's x/y are regionprops pixel-CENTRE;
    # QuPath reads pixel-CORNER, and so does FlowPath when it re-exports these
    # values (join_flowpath.py inverts exactly this expression).
    x_px = centre_to_corner(float(row.get("x", 0)))
    y_px = centre_to_corner(float(row.get("y", 0)))
    measurements.append({"name": "Centroid X µm", "value": round(x_px * pixel_size, 3)})
    measurements.append({"name": "Centroid Y µm", "value": round(y_px * pixel_size, 3)})

    # Raw marker intensities
    for col in marker_cols:
        val = row.get(col)
        if pd.notna(val):
            measurements.append({"name": col, "value": round(float(val), 4)})

    # Morphological features (converted to µm where appropriate)
    area = row.get("area")
    if pd.notna(area):
        measurements.append(
            {
                "name": "Area µm²",
                "value": round(float(area) * pixel_size * pixel_size, 3),
            }
        )

    # Length measurements converted to µm, area measurements to µm²
    # Dimensionless ratios (eccentricity, solidity) kept as-is
    for morph_col, display_name, unit_factor in [
        ("eccentricity", "Eccentricity", 1.0),
        ("perimeter", "Perimeter µm", pixel_size),
        ("solidity", "Solidity", 1.0),
        ("convex_area", "Convex Area µm²", pixel_size * pixel_size),
        ("axis_major_length", "Major Axis Length µm", pixel_size),
        ("axis_minor_length", "Minor Axis Length µm", pixel_size),
    ]:
        val = row.get(morph_col)
        if pd.notna(val):
            measurements.append(
                {"name": display_name, "value": round(float(val) * unit_factor, 4)}
            )

    return measurements


def _polygon_geometry(
    contours: Optional[Dict[str, List[List[float]]]],
    label_str: str,
) -> Optional[Dict]:
    """Return a GeoJSON Polygon geometry for a label, or None if no contour exists."""
    if contours and label_str in contours:
        return {"type": "Polygon", "coordinates": [contours[label_str]]}
    return None


def build_feature(
    measurements: List[Dict],
    geometry: Dict,
    color_int: int,
    object_type: str = "detection",
    nucleus_geometry: Optional[Dict] = None,
    object_id: Optional[str] = "PathDetectionObject",
) -> Dict:
    """Assemble a single QuPath-native GeoJSON Feature.

    ``nucleus_geometry``, when provided, is written as a **top-level** member of the
    Feature (sibling of ``geometry``), exactly matching QuPath's own serialization
    of cell objects (qupath.lib.io.QuPathTypeAdapters). It is only meaningful when
    ``object_type == "cell"``.
    """
    feature: Dict = {"type": "Feature"}
    if object_id is not None:
        feature["id"] = object_id
    feature["geometry"] = geometry
    if nucleus_geometry is not None:
        feature["nucleusGeometry"] = nucleus_geometry
    feature["properties"] = {
        "objectType": object_type,
        "classification": {"name": "Cell", "colorRGB": color_int},
        "isLocked": False,
        "measurements": measurements,
    }
    return feature


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
        x_px = row.get("x")
        y_px = row.get("y")
        if pd.isna(x_px) or pd.isna(y_px):
            skipped += 1
            continue

        # Match the pixel-CORNER convention the polygon contours are already in
        # (regionprops centroids are pixel-CENTRE; extract_cell_properties writes
        # contours.json shifted for QuPath/ImageJ).
        x_px_corner = centre_to_corner(float(x_px))
        y_px_corner = centre_to_corner(float(y_px))

        cell_id = row.get("label", idx)
        cell_label_str = str(int(cell_id)) if pd.notna(cell_id) else str(idx)

        # Geometry: Polygon if contour available, else Point
        geometry = _polygon_geometry(contours, cell_label_str) or {
            "type": "Point",
            "coordinates": [x_px_corner, y_px_corner],
        }

        # Build measurements array (QuPath native format)
        measurements = build_measurements(row, marker_cols, pixel_size)
        # object_id=None: do not stamp every cell with the same constant id
        # ("PathDetectionObject"), which QuPath treats as a per-object UUID — a
        # shared id across all detections breaks re-import. (Matches the
        # compartment export path, which already passes object_id=None.)
        features.append(
            build_feature(measurements, geometry, color_int, object_id=None)
        )

    logger.info(f"  Exported {len(features)} cells, skipped {skipped}")

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    with open(output_path, "w") as f:
        json.dump(geojson, f)

    file_size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    logger.info(f"  GeoJSON size: {file_size_mb:.1f} MB")

    return len(features)


def _write_collection(features: List[Dict], output_path: str) -> None:
    """Write a list of features as a GeoJSON FeatureCollection."""
    with open(output_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


def export_combined_geojson(
    df: pd.DataFrame,
    output_dir: str,
    pixel_size: float,
    marker_cols: List[str],
    cell_contours: Optional[Dict[str, List[List[float]]]],
    nucleus_contours: Optional[Dict[str, List[List[float]]]],
    prefix: str = "cells",
) -> Dict[str, int]:
    """Export one combined GeoJSON for per-compartment quantification.

    Writes a single ``<prefix>.geojson`` of QuPath-native **cell** objects: the
    whole-cell polygon as ``geometry`` and the nucleus polygon as top-level
    ``nucleusGeometry``, carrying all per-compartment measurements. This is the
    file FlowPath / qUMAP / annomask consume; QuPath toggles the drawn outline
    (nucleus vs cell) natively, so no separate nuclei/whole-cell files are needed.

    ``nucleus_contours`` must be keyed by **cell label** (re-keyed upstream by
    EXTRACT_NUCLEI_PROPERTIES via ``--reference_mask``), so lookup is a plain
    identity on the cell label.
    """
    color_int = rgb_to_qupath_color(*CELL_COLOR_RGB)

    cells_combined: List[Dict] = []
    n_with_nucleus = 0
    skipped = 0

    for idx, row in df.iterrows():
        x_px = row.get("x")
        y_px = row.get("y")
        if pd.isna(x_px) or pd.isna(y_px):
            skipped += 1
            continue
        # pixel-CENTRE -> QuPath pixel-CORNER, as above.
        x_corner = centre_to_corner(float(x_px))
        y_corner = centre_to_corner(float(y_px))

        cell_id = row.get("label", idx)
        label_str = str(int(cell_id)) if pd.notna(cell_id) else str(idx)

        cell_geom = _polygon_geometry(cell_contours, label_str) or {
            "type": "Point",
            "coordinates": [x_corner, y_corner],
        }
        nucleus_geom = _polygon_geometry(nucleus_contours, label_str)
        if nucleus_geom is not None:
            n_with_nucleus += 1

        measurements = build_measurements(row, marker_cols, pixel_size)

        cells_combined.append(
            build_feature(
                measurements,
                cell_geom,
                color_int,
                object_type="cell",
                nucleus_geometry=nucleus_geom,
                object_id=None,
            )
        )

    out = Path(output_dir)
    combined_path = str(out / f"{prefix}.geojson")
    _write_collection(cells_combined, combined_path)

    # Whole-cell-only companion: same cells and the SAME measurements, but WITHOUT
    # the nucleusGeometry polygon. Dropping the second polygon roughly halves the
    # geometry, so it imports much faster in QuPath; compartment gating still works
    # in FlowPath (which reads the measurement keys, not the nucleus polygon). Import
    # whichever you need — <prefix>.geojson for the nucleus outline / cell-vs-nucleus
    # toggle, <prefix>_wholecell.geojson for a fast, lean import.
    wholecell = [
        {k: v for k, v in feat.items() if k != "nucleusGeometry"}
        for feat in cells_combined
    ]
    wholecell_path = str(out / f"{prefix}_wholecell.geojson")
    _write_collection(wholecell, wholecell_path)

    counts = {prefix: len(cells_combined), f"{prefix}_wholecell": len(wholecell)}
    logger.info(
        f"  Combined export: {len(cells_combined)} cell objects "
        f"({n_with_nucleus} with nucleus), skipped {skipped}"
    )
    logger.info(
        f"  Whole-cell companion: {len(wholecell)} cells without nucleusGeometry "
        f"-> {Path(wholecell_path).name}"
    )
    return counts


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Export cell data to QuPath-compatible GeoJSON",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--cell_data",
        required=True,
        help="Path to merged quantification CSV (from MERGE_QUANT_CSVS)",
    )
    parser.add_argument(
        "-o",
        "--output_dir",
        required=True,
        help="Output directory",
    )
    parser.add_argument(
        "--contours_json",
        type=str,
        default=None,
        help="Path to pre-computed whole-cell contours JSON (from extract_cell_properties.py)",
    )
    parser.add_argument(
        "--nucleus_contours_json",
        type=str,
        default=None,
        help="Path to nucleus contours JSON re-keyed to cell labels (from "
        "EXTRACT_NUCLEI_PROPERTIES). When given, each cell in the single combined "
        "cells.geojson gets a top-level nucleusGeometry field for per-compartment quantification.",
    )
    parser.add_argument(
        "--pixel-size",
        type=float,
        default=0.325,
        help="Pixel size in micrometers",
    )
    parser.add_argument(
        "--output_prefix",
        default="cells",
        help="Prefix for output files",
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
    if "label" in cell_df.columns:
        cell_df = cell_df.drop_duplicates(subset="label", keep="first")

    logger.info(f"Loaded {len(cell_df)} cells")

    # Identify marker columns
    marker_cols = identify_marker_columns(cell_df)
    logger.info(f"Detected {len(marker_cols)} marker channels: {marker_cols}")

    # Load whole-cell contours
    contours = None
    if args.contours_json:
        logger.info(f"Loading cell contours: {args.contours_json}")
        with open(args.contours_json, "r") as f:
            contours = json.load(f)
        logger.info(f"Loaded contours for {len(contours)} cells")

    # Load nucleus contours (re-keyed to cell labels) for the combined per-compartment export
    nucleus_contours = None
    if args.nucleus_contours_json:
        logger.info(f"Loading nucleus contours: {args.nucleus_contours_json}")
        with open(args.nucleus_contours_json, "r") as f:
            nucleus_contours = json.load(f)
        logger.info(f"Loaded nucleus contours for {len(nucleus_contours)} cells")

    # Compute z-scores for CSV output
    cell_df_with_z = compute_zscores(cell_df, marker_cols)

    output_geojson = str(Path(args.output_dir) / f"{args.output_prefix}.geojson")
    if nucleus_contours is not None:
        # Per-compartment quantification: one combined cell+nucleus GeoJSON.
        counts = export_combined_geojson(
            df=cell_df,
            output_dir=args.output_dir,
            pixel_size=args.pixel_size,
            marker_cols=marker_cols,
            cell_contours=contours,
            nucleus_contours=nucleus_contours,
            prefix=args.output_prefix,
        )
        num_exported = counts[args.output_prefix]
    else:
        # Whole-cell-only export (legacy behaviour).
        num_exported = export_geojson(
            df=cell_df,
            output_path=output_geojson,
            pixel_size=args.pixel_size,
            marker_cols=marker_cols,
            contours=contours,
        )

    # Save CSV with both raw and z-score columns
    output_csv = str(Path(args.output_dir) / f"{args.output_prefix}_data.csv")
    logger.info(f"Saving CSV: {output_csv}")
    csv_df = cell_df_with_z.copy()
    csv_df.to_csv(output_csv, index=False)

    # Summary
    logger.info("=" * 80)
    logger.info("EXPORT COMPLETED SUCCESSFULLY")
    logger.info("=" * 80)
    logger.info("Output files:")
    logger.info(f"  GeoJSON: {output_geojson} ({num_exported} cells)")
    logger.info(
        f"  CSV:     {output_csv} ({len(csv_df)} cells, {len(csv_df.columns)} columns)"
    )
    logger.info("")
    logger.info("To view in QuPath:")
    logger.info("  1. Open your pyramidal OME-TIFF image")
    logger.info("  2. Run import_geojson.groovy or use File > Import objects")
    logger.info("  3. Open FlowPath for interactive gating")
    logger.info("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
