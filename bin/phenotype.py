#!/usr/bin/env python3
"""Cell phenotyping based on marker expression.

This module assigns phenotype labels to cells based on marker expression
thresholds and exports results in QuPath-compatible GeoJSON format.

Outputs:
    - phenotypes_data.csv: Full cell data with phenotype assignments
    - phenotypes.geojson: QuPath-compatible cell detections with classifications
    - phenotypes.classifications.json: Color definitions for QuPath
"""

from __future__ import annotations

import argparse
import colorsys
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
from scipy.stats import zscore
from numpy.typing import NDArray

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / 'utils'))

from logger import get_logger, configure_logging
from image_utils import ensure_dir

logger = get_logger(__name__)


__all__ = [
    "run_phenotyping_pipeline",
    "export_to_geojson",
]


# =============================================================================
# CONFIGURATION
# =============================================================================

# Phenotype colors for QuPath visualization (RGB) - used as fallback
PHENOTYPE_COLORS = {
    "Background": (0, 0, 0),
    "Unknown": (64, 64, 64),
    "Immune": (0, 255, 0),
    "T helper": (255, 255, 0),
    "T cytotoxic": (0, 255, 255),
    "activated T cytotoxic": (0, 200, 255),
    "CD4 T regulatory": (255, 200, 0),
    "CD8 T regulatory": (200, 255, 0),
    "Macrophages": (255, 128, 0),
    "M1": (255, 80, 80),
    "M2": (255, 160, 80),
    "PANCK+ Tumor": (255, 0, 0),
    "VIM+ Tumor": (255, 0, 255),
    "Stroma": (128, 128, 128),
}

# Global variable to hold custom colors from config (set at runtime)
_CUSTOM_COLORS: Dict[str, Tuple[int, int, int]] = {}


# =============================================================================
# CONFIG LOADING
# =============================================================================

def load_phenotype_config(config_path: str) -> dict:
    """Load and validate phenotype configuration from JSON file.

    Parameters
    ----------
    config_path : str
        Path to the phenotype configuration JSON file.

    Returns
    -------
    config : dict
        Validated configuration with keys:
        - thresholds: dict mapping marker names to z-score cutoffs
        - phenotypes: list of phenotype definitions (sorted by priority)
        - colors: dict mapping phenotype names to RGB tuples (optional)

    Raises
    ------
    ValueError
        If required keys are missing from the config.
    FileNotFoundError
        If the config file does not exist.
    """
    logger.info(f"Loading phenotype config: {config_path}")

    with open(config_path, 'r') as f:
        config = json.load(f)

    # Validate required keys
    required = ['thresholds', 'phenotypes']
    for key in required:
        if key not in config:
            raise ValueError(f"Phenotype config missing required key: '{key}'")

    # Validate thresholds
    if not isinstance(config['thresholds'], dict):
        raise ValueError("'thresholds' must be a dictionary mapping marker names to cutoff values")

    # Validate phenotypes
    if not isinstance(config['phenotypes'], list):
        raise ValueError("'phenotypes' must be a list of phenotype definitions")

    for i, pheno in enumerate(config['phenotypes']):
        if 'name' not in pheno:
            raise ValueError(f"Phenotype at index {i} missing required 'name' field")
        if 'rules' not in pheno:
            raise ValueError(f"Phenotype '{pheno['name']}' missing required 'rules' field")
        if not isinstance(pheno['rules'], dict):
            raise ValueError(f"Phenotype '{pheno['name']}' rules must be a dictionary")
        # Validate rule values
        for marker, status in pheno['rules'].items():
            if status not in ('+', '-'):
                raise ValueError(
                    f"Phenotype '{pheno['name']}' has invalid rule for '{marker}': "
                    f"expected '+' or '-', got '{status}'"
                )

    # Sort phenotypes by priority (lower priority applied first, higher overrides)
    config['phenotypes'] = sorted(
        config['phenotypes'],
        key=lambda x: x.get('priority', 0)
    )

    # Convert colors from list to tuple if present
    if 'colors' in config:
        global _CUSTOM_COLORS
        _CUSTOM_COLORS = {}
        for name, rgb in config['colors'].items():
            if isinstance(rgb, list) and len(rgb) == 3:
                _CUSTOM_COLORS[name] = tuple(rgb)
            else:
                logger.warning(f"Invalid color for '{name}': {rgb}, expected [R, G, B]")

    logger.info(f"  Loaded {len(config['thresholds'])} marker thresholds")
    logger.info(f"  Loaded {len(config['phenotypes'])} phenotype definitions")
    if 'colors' in config:
        logger.info(f"  Loaded {len(config.get('colors', {}))} custom colors")

    return config


def apply_phenotype_rules(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Apply phenotype classification rules from config to dataframe.

    This function marks cells as positive/negative for each marker based on
    z-score thresholds, then assigns phenotypes based on marker combinations.

    Parameters
    ----------
    df : pd.DataFrame
        Cell data with z-score normalized marker columns.
    config : dict
        Phenotype configuration with 'thresholds' and 'phenotypes' keys.

    Returns
    -------
    pd.DataFrame
        Input dataframe with added columns:
        - pheno_markers: list of positive markers for each cell
        - phenotype: assigned phenotype name
    """
    thresholds = config['thresholds']
    phenotypes = config['phenotypes']

    logger.info("Applying phenotype rules from config")
    logger.info(f"  Thresholds: {thresholds}")

    # Initialize marker positivity tracking
    df['pheno_markers'] = [[] for _ in range(len(df))]

    # Mark positive cells for each marker based on thresholds
    for marker, cutoff in thresholds.items():
        if marker in df.columns:
            positive_mask = df[marker] >= cutoff
            positive_count = positive_mask.sum()
            logger.debug(f"  {marker} >= {cutoff}: {positive_count} positive cells")
            for idx in df[positive_mask].index:
                df.at[idx, 'pheno_markers'].append(marker)

    # Apply phenotype rules (sorted by priority, lower first so higher overrides)
    df['phenotype'] = 'Unknown'

    for pheno_def in phenotypes:
        name = pheno_def['name']
        rules = pheno_def['rules']
        priority = pheno_def.get('priority', 0)

        # Build mask: all rules must match (AND logic)
        mask = pd.Series(True, index=df.index)

        for marker, status in rules.items():
            if status == '+':
                # Marker must be positive
                mask &= df['pheno_markers'].apply(lambda x: marker in x)
            elif status == '-':
                # Marker must be negative
                mask &= df['pheno_markers'].apply(lambda x: marker not in x)

        matched_count = mask.sum()
        if matched_count > 0:
            df.loc[mask, 'phenotype'] = name
            logger.debug(f"  {name} (priority {priority}): {matched_count} cells")

    # Log phenotype distribution
    pheno_counts = df['phenotype'].value_counts()
    logger.info(f"Phenotype distribution:\n{pheno_counts}")

    return df


# =============================================================================
# GEOJSON EXPORT FUNCTIONS
# =============================================================================

def rgb_to_qupath_color(r: int, g: int, b: int, a: int = 255) -> int:
    """Convert RGB to QuPath's signed 32-bit ARGB color format."""
    value = (a << 24) | (r << 16) | (g << 8) | b
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def get_phenotype_color(phenotype: str, index: int = 0) -> Tuple[int, int, int]:
    """Get color for a phenotype, generating one if not predefined.

    Checks custom colors from config first, then falls back to defaults.
    """
    # Check custom colors from config first
    if phenotype in _CUSTOM_COLORS:
        return _CUSTOM_COLORS[phenotype]

    # Fall back to built-in defaults
    if phenotype in PHENOTYPE_COLORS:
        return PHENOTYPE_COLORS[phenotype]

    # Generate deterministic color for unknown phenotypes
    h = (index * 0.618033988749895) % 1.0
    s = 0.7 + (index % 3) * 0.1
    v = 0.85 + (index % 2) * 0.1
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def export_to_geojson(
    df: pd.DataFrame,
    output_path: str,
    pixel_size: float = 0.325,
    x_col: str = 'x',
    y_col: str = 'y',
    phenotype_col: str = 'phenotype',
    cell_id_col: str = 'label',
    exclude_background: bool = True,
    measurement_cols: Optional[List[str]] = None,
    contours: Optional[Dict[str, List[List[float]]]] = None,
) -> Tuple[int, Dict]:
    """
    Export phenotyped cells to QuPath-compatible GeoJSON.
    
    Parameters
    ----------
    df : DataFrame
        Cell data with coordinates and phenotype assignments.
    output_path : str
        Path for output GeoJSON file.
    pixel_size : float
        Micrometers per pixel for coordinate conversion.
    x_col : str
        Column name for X centroid (in pixels).
    y_col : str
        Column name for Y centroid (in pixels).
    phenotype_col : str
        Column name for phenotype classification.
    cell_id_col : str
        Column name for cell ID/label.
    exclude_background : bool
        Skip cells with phenotype "Background" or "Unknown".
    measurement_cols : list, optional
        Columns to include as measurements. If None, includes all numeric columns.
    contours : dict, optional
        Pre-computed cell contours mapping str(label) -> [[x,y], ...] polygon ring.
        If provided, cells with contours get Polygon geometry; others fall back to Point.

    Returns
    -------
    num_exported : int
        Number of cells exported.
    phenotype_colors : dict
        Mapping of phenotype names to RGB colors.
    """
    logger.info(f"Exporting {len(df)} cells to GeoJSON: {output_path}")
    
    # Determine measurement columns
    if measurement_cols is None:
        exclude_cols = {x_col, y_col, phenotype_col, cell_id_col, 'phenotype_num', 'pheno_markers'}
        measurement_cols = [
            col for col in df.columns 
            if col not in exclude_cols and pd.api.types.is_numeric_dtype(df[col])
        ]
    
    # Get unique phenotypes and assign colors
    phenotypes = df[phenotype_col].unique()
    phenotype_color_map = {}
    for i, pheno in enumerate(phenotypes):
        phenotype_color_map[pheno] = get_phenotype_color(pheno, i)
    
    # Build GeoJSON features
    features = []
    skipped = 0
    
    for idx, row in df.iterrows():
        phenotype = row[phenotype_col]
        
        # Skip background/unknown if requested
        if exclude_background and phenotype in ('Background', 'Unknown'):
            skipped += 1
            continue
        
        # Get coordinates (keep in pixels for QuPath)
        x_px = row[x_col]
        y_px = row[y_col]

        if pd.isna(x_px) or pd.isna(y_px):
            skipped += 1
            continue

        x = float(x_px)
        y = float(y_px)
        
        # Get cell ID
        cell_id = row.get(cell_id_col, idx)
        
        # Build measurements dict (convert to µm for display)
        measurements = {
            "Centroid X µm": round(x * pixel_size, 3),
            "Centroid Y µm": round(y * pixel_size, 3),
        }

        for col in measurement_cols:
            if col in row and pd.notna(row[col]):
                try:
                    val = float(row[col])
                    # Convert area to µm² if it's an area column
                    if 'area' in col.lower():
                        measurements[f"{col} µm²"] = round(val * pixel_size * pixel_size, 3)
                    else:
                        measurements[col] = round(val, 4)
                except (ValueError, TypeError):
                    pass

        # Determine geometry: Polygon if contour available, else Point
        cell_label_str = str(int(cell_id)) if pd.notna(cell_id) else str(idx)
        if contours and cell_label_str in contours:
            geometry = {
                "type": "Polygon",
                "coordinates": [contours[cell_label_str]],
            }
        else:
            geometry = {
                "type": "Point",
                "coordinates": [x, y],
            }

        # Create GeoJSON feature (coordinates in pixels for QuPath)
        feature = {
            "type": "Feature",
            "id": str(cell_id),
            "geometry": geometry,
            "properties": {
                "objectType": "detection",
                "classification": {
                    "name": phenotype
                },
                "measurements": measurements
            }
        }
        
        features.append(feature)
    
    logger.info(f"  Exported {len(features)} cells, skipped {skipped}")
    
    # Create GeoJSON FeatureCollection
    geojson = {
        "type": "FeatureCollection",
        "features": features
    }
    
    # Save GeoJSON
    with open(output_path, 'w') as f:
        json.dump(geojson, f)
    
    file_size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    logger.info(f"  GeoJSON size: {file_size_mb:.1f} MB")
    
    return len(features), phenotype_color_map


def export_classifications(
    phenotype_colors: Dict[str, Tuple[int, int, int]],
    output_path: str,
    exclude_background: bool = True
):
    """
    Export phenotype classifications JSON for QuPath color setup.
    
    Parameters
    ----------
    phenotype_colors : dict
        Mapping of phenotype name to (R, G, B) tuple.
    output_path : str
        Output path for classifications JSON.
    exclude_background : bool
        Exclude "Background" from output.
    """
    classifications = []
    
    for name, rgb in phenotype_colors.items():
        if exclude_background and name in ('Background', 'Unknown'):
            continue
        
        r, g, b = rgb
        color_int = rgb_to_qupath_color(r, g, b)
        
        classifications.append({
            "name": name,
            "color": color_int,
            "rgb": [r, g, b],
            "hex": f"#{r:02x}{g:02x}{b:02x}"
        })
    
    with open(output_path, 'w') as f:
        json.dump(classifications, f, indent=2)
    
    logger.info(f"Saved {len(classifications)} classifications to {output_path}")


# =============================================================================
# PHENOTYPING PIPELINE
# =============================================================================

def run_phenotyping_pipeline(
    cell_df: pd.DataFrame,
    config: dict,
    quality_percentile: float = 1.0,
    noise_percentile: float = 0.01
) -> Tuple[pd.DataFrame, Dict[int, str]]:
    """Run cell phenotyping pipeline.

    Parameters
    ----------
    cell_df : DataFrame
        Cell data with marker intensities and morphological features.
    config : dict
        Phenotype configuration with 'thresholds' and 'phenotypes' keys.
    quality_percentile : float, optional
        Percentile for quality filtering.
    noise_percentile : float, optional
        Percentile for noise removal.

    Returns
    -------
    phenotypes_df : DataFrame
        Cell data with phenotype assignments.
    phenotype_mapping : dict
        Mapping of phenotype number to name.
    """
    markers = list(config['thresholds'].keys())

    logger.info("Starting phenotyping pipeline")
    logger.info(f"Markers: {markers}")
    logger.info(f"Number of cells: {len(cell_df)}")

    # quality_percentile and noise_percentile are used directly with np.percentile
    # e.g., quality_percentile=1 means filter at the 1st percentile (remove bottom 1%)

    # Reorder columns (keep available ones)
    base_cols = ['y', 'x', 'eccentricity', 'perimeter', 'convex_area', 'area',
                 'axis_major_length', 'axis_minor_length', 'label']

    # Get marker columns that exist in the dataframe
    available_markers = [m for m in markers if m in cell_df.columns]
    if 'DAPI' in cell_df.columns:
        available_markers.append('DAPI')

    available_cols = [c for c in base_cols if c in cell_df.columns]
    all_cols = available_cols + available_markers

    cell_df = cell_df[[c for c in all_cols if c in cell_df.columns]].copy()

    # Quality filtering
    logger.info("Applying quality filters")
    if 'DAPI' in cell_df.columns:
        nuc_thres = np.percentile(cell_df['DAPI'], quality_percentile)
        size_thres = np.percentile(cell_df['area'], quality_percentile)
        cell_df_filtered = cell_df[
            (cell_df['DAPI'] >= nuc_thres) & (cell_df['area'] >= size_thres)
        ].copy()
    else:
        size_thres = np.percentile(cell_df['area'], quality_percentile)
        cell_df_filtered = cell_df[cell_df['area'] >= size_thres].copy()

    logger.info(f"Cells after quality filter: {len(cell_df_filtered)}")

    if cell_df_filtered.empty:
        logger.warning("No cells passed quality filtering")
        empty_df = cell_df_filtered.copy()
        empty_df['pheno_markers'] = [[] for _ in range(len(empty_df))]
        empty_df['phenotype'] = pd.Series(dtype=str)
        empty_df['phenotype_num'] = pd.Series(dtype=int)
        empty_df['pheno_markers_str'] = pd.Series(dtype=str)
        return empty_df, {0: "Background"}

    # Normalization
    logger.info("Normalizing marker intensities")
    list_out = ['eccentricity', 'perimeter', 'convex_area',
                'axis_major_length', 'axis_minor_length']
    list_keep = ['DAPI', 'x', 'y', 'area', 'label']

    dfin = cell_df_filtered.drop(
        [c for c in list_out if c in cell_df_filtered.columns],
        axis=1
    )
    df_loc = dfin[[c for c in list_keep if c in dfin.columns]]
    dfz = dfin.drop([c for c in list_keep if c in dfin.columns], axis=1)

    # Z-score normalization
    if len(dfz.columns) > 0:
        dfz1 = pd.DataFrame(
            zscore(dfz, axis=0),
            index=dfz.index,
            columns=dfz.columns
        )
        dfz_all = pd.concat([dfz1, df_loc], axis=1, join="inner")
    else:
        dfz_all = df_loc.copy()

    # Noise removal
    logger.info("Removing noise")
    last_marker = 'VIMENTIN' if 'VIMENTIN' in dfz_all.columns else (
        available_markers[-1] if available_markers else None
    )
    
    df_nn = dfz_all.copy()
    
    if last_marker and last_marker in dfz_all.columns:
        col_num_last_marker = dfz_all.columns.get_loc(last_marker)
        df_nn["Count"] = dfz_all.iloc[:, :col_num_last_marker + 1].ge(0).sum(axis=1)
        df_nn["z_sum"] = dfz_all.iloc[:, :col_num_last_marker + 1].sum(axis=1)

    # Phenotyping based on marker expression using config rules
    logger.info("Assigning phenotypes")
    df_nn = apply_phenotype_rules(df_nn, config)

    # Add numeric phenotype labels
    pheno_complete = df_nn['phenotype'].value_counts().index.values
    phenotype_mapping = {0: "Background"}
    for pp, p in enumerate(pheno_complete, start=1):
        sel = df_nn[df_nn['phenotype'] == p].index
        df_nn.loc[sel, 'phenotype_num'] = pp
        phenotype_mapping[pp] = p

    df_nn['phenotype_num'] = df_nn['phenotype_num'].fillna(0).astype(int)

    logger.info(f"Phenotype distribution:\n{df_nn['phenotype'].value_counts()}")
    logger.info(f"Phenotype mapping: {phenotype_mapping}")

    # Convert pheno_markers list to string for CSV export
    df_nn['pheno_markers_str'] = df_nn['pheno_markers'].apply(lambda x: ','.join(x) if x else '')

    return df_nn, phenotype_mapping


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Cell phenotyping with QuPath GeoJSON export',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Required arguments
    parser.add_argument(
        '--cell_data',
        required=True,
        help='Path to input CSV file with cell data (from regionprops)'
    )
    parser.add_argument(
        '-o', '--output_dir',
        required=True,
        help='Output directory'
    )

    # Pixel size for coordinate conversion
    parser.add_argument(
        '--pixel_size',
        type=float,
        default=0.325,
        help='Pixel size in micrometers (for GeoJSON coordinate conversion)'
    )

    # Column name overrides
    parser.add_argument(
        '--x_col',
        default='x',
        help='Column name for X centroid (pixels)'
    )
    parser.add_argument(
        '--y_col',
        default='y',
        help='Column name for Y centroid (pixels)'
    )
    parser.add_argument(
        '--cell_id_col',
        default='label',
        help='Column name for cell ID'
    )

    # Phenotyping parameters
    parser.add_argument(
        '--config',
        type=str,
        required=True,
        help='Path to phenotype configuration JSON file (thresholds and rules)'
    )
    parser.add_argument(
        '--quality_percentile',
        type=float,
        default=1.0,
        help='Percentile threshold for quality filtering — cells below this percentile of DAPI/area are removed (e.g., 1 = remove bottom 1%%)'
    )
    parser.add_argument(
        '--noise_percentile',
        type=float,
        default=0.01,
        help='Percentile for noise removal'
    )

    # Cell contours for polygon GeoJSON
    parser.add_argument(
        '--contours_json',
        type=str,
        default=None,
        help='Path to pre-computed contours JSON (from extract_cell_properties.py)'
    )

    # Output options
    parser.add_argument(
        '--include_unknown',
        action='store_true',
        help='Include Unknown phenotype cells in GeoJSON output'
    )
    parser.add_argument(
        '--output_prefix',
        default='phenotypes',
        help='Prefix for output files'
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    configure_logging()
    args = parse_args()

    ensure_dir(args.output_dir)

    logger.info("=" * 80)
    logger.info("Cell Phenotyping Pipeline with QuPath Export")
    logger.info("=" * 80)

    # Load data
    logger.info(f"Loading cell data: {args.cell_data}")
    cell_df = pd.read_csv(args.cell_data)
    
    # Remove duplicates by label if present
    if args.cell_id_col in cell_df.columns:
        cell_df = cell_df.drop_duplicates(subset=args.cell_id_col, keep='first')
    
    logger.info(f"Loaded {len(cell_df)} cells")

    # Load phenotype config (required)
    config = load_phenotype_config(args.config)

    # Load pre-computed contours if provided
    contours = None
    if args.contours_json:
        logger.info(f"Loading contours: {args.contours_json}")
        with open(args.contours_json, 'r') as f:
            contours = json.load(f)
        logger.info(f"Loaded contours for {len(contours)} cells")

    # Run phenotyping
    phenotypes_df, phenotype_mapping = run_phenotyping_pipeline(
        cell_df,
        config=config,
        quality_percentile=args.quality_percentile,
        noise_percentile=args.noise_percentile
    )

    # Define output paths
    output_csv = os.path.join(args.output_dir, f'{args.output_prefix}_data.csv')
    output_geojson = os.path.join(args.output_dir, f'{args.output_prefix}.geojson')
    output_classifications = os.path.join(args.output_dir, f'{args.output_prefix}.classifications.json')
    output_mapping = os.path.join(args.output_dir, f'{args.output_prefix}_mapping.json')

    # Save CSV (convert pheno_markers list to string for CSV compatibility)
    logger.info(f"Saving phenotype data: {output_csv}")
    csv_df = phenotypes_df.copy()
    if 'pheno_markers' in csv_df.columns:
        csv_df = csv_df.drop(columns=['pheno_markers'])
    csv_df.to_csv(output_csv, index=False)

    # Export GeoJSON for QuPath
    logger.info(f"Exporting GeoJSON: {output_geojson}")
    num_exported, phenotype_colors = export_to_geojson(
        df=phenotypes_df,
        output_path=output_geojson,
        pixel_size=args.pixel_size,
        x_col=args.x_col,
        y_col=args.y_col,
        phenotype_col='phenotype',
        cell_id_col=args.cell_id_col,
        exclude_background=not args.include_unknown,
        contours=contours,
    )

    # Export classifications JSON (for QuPath color setup)
    logger.info(f"Saving classifications: {output_classifications}")
    export_classifications(
        phenotype_colors=phenotype_colors,
        output_path=output_classifications,
        exclude_background=not args.include_unknown
    )

    # Save phenotype mapping
    logger.info(f"Saving phenotype mapping: {output_mapping}")
    with open(output_mapping, 'w') as f:
        json.dump(phenotype_mapping, f, indent=2)

    # Summary
    logger.info("=" * 80)
    logger.info("PIPELINE COMPLETED SUCCESSFULLY")
    logger.info("=" * 80)
    logger.info(f"Output files:")
    logger.info(f"  CSV:             {output_csv}")
    logger.info(f"  GeoJSON:         {output_geojson} ({num_exported} cells)")
    logger.info(f"  Classifications: {output_classifications}")
    logger.info(f"  Mapping:         {output_mapping}")
    logger.info("")
    logger.info("To view in QuPath:")
    logger.info("  1. Open your pyramidal OME-TIFF image")
    logger.info("  2. Run the import_phenotypes_simple.groovy script")
    logger.info(f"  3. Select: {output_geojson}")
    logger.info("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
