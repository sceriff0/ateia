#!/usr/bin/env python3
"""Merge per-channel quantification CSVs with morphology into a single table.

Combines morphology (from EXTRACT_CELL_PROPERTIES) with per-marker intensity
CSVs (from QUANTIFY) by joining on the cell label column. Validates that no
cells are lost during the merge and reorders columns for downstream analysis.

Output columns are ordered: fov, cell_size, morphology columns, DAPI, then
remaining markers sorted alphabetically.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / 'utils'))

from logger import configure_logging, get_logger

logger = get_logger(__name__)

# Canonical morphology column order
MORPHOLOGY_COLS = [
    'label', 'y', 'x', 'area', 'eccentricity', 'perimeter',
    'convex_area', 'axis_major_length', 'axis_minor_length', 'solidity',
]


def load_intensity_csvs(csvs_dir: Path | None = None, csv_files_list: list[str] | None = None) -> list[Path]:
    """Find and return sorted list of per-channel quantification CSVs.

    Either provide explicit file paths via csv_files_list, or a directory
    to glob via csvs_dir.
    """
    if csv_files_list:
        csv_files = [Path(f) for f in csv_files_list]
    elif csvs_dir is not None:
        csv_files = sorted(csvs_dir.glob('*_quant.csv'))
    else:
        csv_files = []
    if not csv_files:
        logger.error("No quantification CSVs found")
        sys.exit(1)
    return csv_files


def merge_intensities(
    morphology: pd.DataFrame,
    csv_files: list[Path],
) -> pd.DataFrame:
    """Merge each intensity CSV into the morphology table by label."""
    merged = morphology.copy()
    morphology_cells = set(merged['label'])

    for csv_file in csv_files:
        df = pd.read_csv(csv_file)
        marker_cols = [col for col in df.columns if col != 'label']

        if not marker_cols:
            logger.warning("%s: No marker columns found, skipping", csv_file.name)
            continue

        # Validate cell labels
        intensity_cells = set(df['label'])
        missing = morphology_cells - intensity_cells
        extra = intensity_cells - morphology_cells

        if missing:
            logger.warning("%s: Missing %d cells from morphology", csv_file.name, len(missing))
        if extra:
            logger.warning("%s: Has %d extra cells (will be ignored)", csv_file.name, len(extra))

        merge_df = df[['label'] + marker_cols]
        merged = merged.merge(merge_df, on='label', how='left')

        # Leave missing intensities as NaN (not 0.0) so downstream tools can
        # distinguish genuinely zero intensity from cells absent in this channel
        # (e.g. cells excluded by min_area filter in quantify.py)

        logger.info("  + %s from %s", ', '.join(marker_cols), csv_file.name)

    return merged


def reorder_columns(merged: pd.DataFrame, patient_id: str) -> pd.DataFrame:
    """Reorder columns and add fov/cell_size for downstream analysis."""
    # Separate morphology and marker columns
    morpho_present = [col for col in MORPHOLOGY_COLS if col in merged.columns]
    marker_cols_all = [col for col in merged.columns if col not in MORPHOLOGY_COLS and col not in ('fov', 'cell_size')]

    # Put DAPI first among markers if present
    if 'DAPI' in marker_cols_all:
        marker_cols_all.remove('DAPI')
        final_column_order = morpho_present + ['DAPI'] + sorted(marker_cols_all)
    else:
        final_column_order = morpho_present + sorted(marker_cols_all)
    merged = merged[final_column_order]

    # Add required columns for Pixie cell clustering
    merged['fov'] = patient_id
    if 'area' in merged.columns:
        merged['cell_size'] = merged['area']

    # Move fov and cell_size to the front
    cols = merged.columns.tolist()
    for col_to_move in ['cell_size', 'fov']:
        if col_to_move in cols:
            cols.remove(col_to_move)
            cols = [col_to_move] + cols
    merged = merged[cols]

    return merged


def main() -> None:
    """Parse arguments and run the merge pipeline."""
    parser = argparse.ArgumentParser(
        description='Merge per-channel quantification CSVs with morphology.',
    )
    parser.add_argument(
        '--csvs-dir', type=Path, required=False, default=None,
        help='Directory to glob for *_quant.csv files',
    )
    parser.add_argument(
        '--csv-files', nargs='*', default=None,
        help='Explicit list of CSV files to merge (alternative to --csvs-dir)',
    )
    parser.add_argument(
        '--morphology', type=Path, required=True,
        help='Path to morphology CSV from EXTRACT_CELL_PROPERTIES',
    )
    parser.add_argument(
        '--patient-id', type=str, required=True,
        help='Patient/sample ID for fov column and logging',
    )
    parser.add_argument(
        '--output', type=Path, required=True,
        help='Output path for merged CSV',
    )
    args = parser.parse_args()

    configure_logging()

    logger.info("Sample: %s", args.patient_id)

    # Load morphology (computed once by EXTRACT_CELL_PROPERTIES)
    morphology = pd.read_csv(args.morphology)
    logger.info("Morphology: %d cells, %d columns", len(morphology), len(morphology.columns))

    # Find and merge intensity CSVs
    csv_files = load_intensity_csvs(csvs_dir=args.csvs_dir, csv_files_list=args.csv_files)
    logger.info("Merging %d intensity CSVs with morphology...", len(csv_files))

    merged = merge_intensities(morphology, csv_files)

    # Validate no cells were lost
    cells_lost = len(morphology) - len(merged)
    if cells_lost > 0:
        logger.critical("Lost %d cells during merge", cells_lost)
        sys.exit(1)
    else:
        logger.info("All %d cells preserved", len(merged))

    # Reorder columns and add metadata
    merged = reorder_columns(merged, args.patient_id)

    # Save merged CSV
    merged.to_csv(args.output, index=False)
    logger.info("Merged CSV saved: %d cells, %d columns", len(merged), len(merged.columns))
    logger.info("  Final columns: %s", ', '.join(merged.columns))


if __name__ == '__main__':
    main()
