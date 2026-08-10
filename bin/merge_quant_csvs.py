#!/usr/bin/env python3
"""Merge per-channel quantification CSVs with morphology into a single table.

Combines morphology (from EXTRACT_CELL_PROPERTIES) with per-marker intensity
CSVs (from QUANTIFY) by joining on the cell label column. Validates that no
cells are lost during the merge and reorders columns for downstream analysis.

Output columns are ordered: fov, cell_size, morphology columns, the
nuclear/fiducial marker, then remaining markers sorted alphabetically.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / "utils"))

from logger import configure_logging, get_logger
from measurements import MORPHOLOGY_COLS
from metadata import is_nuclear

logger = get_logger(__name__)

# Canonical morphology column order (single source of truth:
# bin/utils/measurements.py). Kept as a list here since reorder_columns
# below iterates it to build an ordered column selection.
MORPHOLOGY_COLS = list(MORPHOLOGY_COLS)


def load_intensity_csvs(
    csvs_dir: Path | None = None, csv_files_list: list[str] | None = None
) -> list[Path]:
    """Find and return sorted list of per-channel quantification CSVs.

    Either provide explicit file paths via csv_files_list, or a directory
    to glob via csvs_dir.
    """
    if csv_files_list:
        csv_files = [Path(f) for f in csv_files_list]
    elif csvs_dir is not None:
        csv_files = sorted(csvs_dir.glob("*_quant.csv"))
    else:
        csv_files = []
    if not csv_files:
        logger.error("No quantification CSVs found")
        sys.exit(1)
    return csv_files


def merge_intensities(
    morphology: pd.DataFrame,
    csv_files: list[Path],
    protected_cols: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Merge each intensity CSV into the base table by label.

    `morphology` is the base table: a morphology-only table for a normal run,
    or a full prior merged table for an incremental (add_cycle) run. When an
    incoming marker column already exists in the base, the incoming value wins
    (overwrites), UNLESS the column is in `protected_cols` (e.g. DAPI), in which
    case the base is kept and the incoming column is dropped.
    """
    merged = morphology.copy()
    morphology_cells = set(merged["label"])

    for csv_file in csv_files:
        df = pd.read_csv(csv_file)
        marker_cols = [col for col in df.columns if col != "label"]

        if not marker_cols:
            logger.warning("%s: No marker columns found, skipping", csv_file.name)
            continue

        # Collision handling: for any marker already present in the base table.
        for col in list(marker_cols):
            if col in merged.columns:
                if col in protected_cols:
                    df = df.drop(columns=[col])
                    logger.info(
                        "%s: protected column '%s' kept from base (incoming dropped)",
                        csv_file.name,
                        col,
                    )
                else:
                    merged = merged.drop(columns=[col])
                    logger.info(
                        "%s: column '%s' overwritten by new cycle (new wins)",
                        csv_file.name,
                        col,
                    )
        marker_cols = [col for col in df.columns if col != "label"]
        if not marker_cols:
            continue

        # Validate cell labels
        intensity_cells = set(df["label"])
        missing = morphology_cells - intensity_cells
        extra = intensity_cells - morphology_cells
        if missing:
            logger.warning(
                "%s: Missing %d cells from base", csv_file.name, len(missing)
            )
        if extra:
            logger.warning(
                "%s: Has %d extra cells (will be ignored)", csv_file.name, len(extra)
            )

        merge_df = df[["label"] + marker_cols]
        merged = merged.merge(merge_df, on="label", how="left")
        logger.info("  + %s from %s", ", ".join(marker_cols), csv_file.name)

    return merged


def reorder_columns(
    merged: pd.DataFrame, patient_id: str, nuclear_markers=None
) -> pd.DataFrame:
    """Reorder columns and add fov/cell_size for downstream analysis.

    The nuclear/fiducial marker leads the marker block. Which channel that is
    comes from ``nuclear_markers`` (``params.nuclear_markers``, passed down by
    MERGE_QUANT_CSVS) via the shared ``utils.metadata.is_nuclear`` rule -- this
    used to test for the literal string ``"DAPI"``, so a CELLTOX panel silently
    got alphabetical ordering instead. Ordering is presentational (every consumer
    reads by column NAME), but it should not depend on how a panel is named.
    """
    # Separate morphology and marker columns
    morpho_present = [col for col in MORPHOLOGY_COLS if col in merged.columns]
    marker_cols_all = [col for col in merged.columns if col not in MORPHOLOGY_COLS]

    # Put the nuclear/fiducial marker first among markers if present. There is at
    # most one in practice (SPLIT_CHANNELS keeps it from the reference slide only),
    # but sort any ties so the order stays deterministic.
    nuclear = sorted(c for c in marker_cols_all if is_nuclear(c, nuclear_markers))
    rest = sorted(c for c in marker_cols_all if c not in nuclear)
    final_column_order = morpho_present + nuclear + rest
    merged = merged[final_column_order]

    # Add required columns for Pixie cell clustering
    merged["fov"] = patient_id
    if "area" in merged.columns:
        merged["cell_size"] = merged["area"]

    # Move fov and cell_size to the front
    cols = merged.columns.tolist()
    for col_to_move in ["cell_size", "fov"]:
        if col_to_move in cols:
            cols.remove(col_to_move)
            cols = [col_to_move] + cols
    merged = merged[cols]

    return merged


def main() -> None:
    """Parse arguments and run the merge pipeline."""
    parser = argparse.ArgumentParser(
        description="Merge per-channel quantification CSVs with morphology.",
    )
    parser.add_argument(
        "--csvs-dir",
        type=Path,
        required=False,
        default=None,
        help="Directory to glob for *_quant.csv files",
    )
    parser.add_argument(
        "--csv-files",
        nargs="*",
        default=None,
        help="Explicit list of CSV files to merge (alternative to --csvs-dir)",
    )
    parser.add_argument(
        "--morphology",
        type=Path,
        required=True,
        help="Path to morphology CSV from EXTRACT_CELL_PROPERTIES",
    )
    parser.add_argument(
        "--patient-id",
        type=str,
        required=True,
        help="Patient/sample ID for fov column and logging",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for merged CSV",
    )
    parser.add_argument(
        "--nuclear-markers",
        dest="nuclear_markers",
        nargs="+",
        default=None,
        help="Ordered nuclear/fiducial marker names. MERGE_QUANT_CSVS passes "
        "params.nuclear_markers; the default is only for standalone use.",
    )
    args = parser.parse_args()

    configure_logging()

    logger.info("Sample: %s", args.patient_id)

    # Load morphology (computed once by EXTRACT_CELL_PROPERTIES)
    morphology = pd.read_csv(args.morphology)
    logger.info(
        "Morphology: %d cells, %d columns", len(morphology), len(morphology.columns)
    )

    # Find and merge intensity CSVs
    csv_files = load_intensity_csvs(
        csvs_dir=args.csvs_dir, csv_files_list=args.csv_files
    )
    logger.info("Merging %d intensity CSVs with morphology...", len(csv_files))

    # The nuclear/fiducial marker is the protected column: it is identical across
    # cycles, so on an add_cycle run the PRIOR table's copy is authoritative and an
    # incoming one is dropped rather than overwriting it. This was pinned to the
    # literal "DAPI", which made the protection silently inert for any other
    # configured fiducial.
    nuclear_cols = tuple(
        col
        for col in morphology.columns
        if col not in MORPHOLOGY_COLS and is_nuclear(col, args.nuclear_markers)
    )
    merged = merge_intensities(morphology, csv_files, protected_cols=nuclear_cols)

    # Validate the row count is exactly preserved. A left merge on 'label' can
    # both LOSE cells (fewer rows) and FAN OUT (more rows) — the latter happens
    # when an intensity CSV has duplicate labels, silently duplicating cells into
    # the GeoJSON and z-score stats. Guard both directions.
    cells_delta = len(merged) - len(morphology)
    if cells_delta < 0:
        logger.critical("Lost %d cells during merge", -cells_delta)
        sys.exit(1)
    elif cells_delta > 0:
        logger.critical(
            "Merge produced %d rows from %d morphology cells (+%d) — duplicate labels "
            "in an intensity CSV fanned out the join",
            len(merged),
            len(morphology),
            cells_delta,
        )
        sys.exit(1)
    else:
        logger.info("All %d cells preserved", len(merged))

    # Reorder columns and add metadata
    merged = reorder_columns(merged, args.patient_id, args.nuclear_markers)

    # Save merged CSV
    merged.to_csv(args.output, index=False)
    logger.info(
        "Merged CSV saved: %d cells, %d columns", len(merged), len(merged.columns)
    )
    logger.info("  Final columns: %s", ", ".join(merged.columns))


if __name__ == "__main__":
    main()
