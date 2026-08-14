#!/usr/bin/env python3
"""Cell quantification for single-channel processing.

Designed for Nextflow pipelines where each channel is processed separately
and results are merged afterward. Supports both CPU and GPU processing.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from scipy import ndimage

# Add parent directory to path to import lib modules
sys.path.insert(0, str(Path(__file__).parent / "utils"))

# Import from lib for DRY principle
from image_utils import ensure_dir, load_image
from logger import configure_logging, get_logger
from measurements import (
    BASE_STATISTICS,
    COMPARTMENTS,
    DEFAULT_STATISTICS,
    NORMALIZATIONS,
    REDSEA_STATISTIC,
    STATISTICS,
    split_statistic,
    zscore,
)
from redsea import band_sums, compensate

logger = get_logger(__name__)


__all__ = [
    "compute_compartment_intensities",
    "is_redsea_marker",
    "resolve_statistics",
    "quantify_single_channel",
    "run_quantification",
]

# Per-compartment quantification: compartment names in the order they are emitted.
# These become part of the QuPath measurement key "<marker>: <Compartment>: <Stat>".
# (single source of truth: bin/utils/measurements.py)
COMPARTMENT_NAMES = COMPARTMENTS


def resolve_statistics(statistics) -> list:
    """Normalise and validate the requested statistic list.

    Accepts the three shapes a Nextflow parameter arrives in, exactly as
    ``MarkerUtils.markerList`` does for ``params.nuclear_markers`` and for the
    same reason: a bare String iterated as a List yields its CHARACTERS, and a
    one-element comma string is a valid Collection that nothing coerces.

    Matching is case-insensitive on input but the CANONICAL casing from
    ``measurements.STATISTICS`` is what comes back, because these names go
    verbatim into the measurement key that qupath-extension-flowpath parses.
    An unknown name is an error, not a silent drop -- a typo'd ``--statistics
    Meidan`` that quietly produced no Median column would look like a pipeline
    bug months later.
    """
    if statistics is None:
        return list(DEFAULT_STATISTICS)
    if isinstance(statistics, str):
        statistics = [statistics]
    flat = []
    for item in statistics:
        if item is None:
            continue
        # COMMA only, never whitespace: composed names contain a space
        # ("Mean Z", "Median RobustZ"), so a whitespace split shreds them
        # into an unknown statistic "Z".
        flat.extend(part for part in str(item).split(",") if part.strip())
    if not flat:
        return list(DEFAULT_STATISTICS)

    canonical = {s.upper(): s for s in STATISTICS}
    out, seen = [], set()
    for name in flat:
        key = name.strip().upper()
        if key not in canonical:
            raise ValueError(
                f"Unknown statistic {name!r}. Valid: {', '.join(STATISTICS)}"
            )
        real = canonical[key]
        if real not in seen:
            seen.add(real)
            out.append(real)
    # Emit in the canonical order rather than the order asked for, so two runs
    # requesting the same set produce byte-identical column ordering.
    return [s for s in STATISTICS if s in seen]


def is_redsea_marker(channel_name: str, redsea_markers) -> bool:
    """Whether this marker opts in to REDSEA compensation.

    Case-insensitive exact match on the trimmed name, against the list from
    ``params.redsea_markers``. Deliberately NOT the substring match
    ``MarkerUtils.isNuclear`` uses: nuclear detection wants 'DAPI' to catch
    'DAPI_nuclear', whereas here a substring rule would make 'CD4' silently
    select 'CD45' too -- and compensating the wrong marker produces plausible
    numbers, not an error.

    REDSEA is only valid for markers whose signal sits on the membrane and is
    brighter in the source cell than in the leak (the paper's own stated
    assumption). Nuclear and diffuse-cytoplasmic markers violate it, which is why
    this is an explicit opt-in list rather than "every channel".
    """
    if not redsea_markers:
        return False
    if isinstance(redsea_markers, str):
        redsea_markers = redsea_markers.split(",")
    wanted = {m.strip().upper() for m in redsea_markers if m and m.strip()}
    return channel_name.strip().upper() in wanted


def _safe_mean(sums: NDArray, counts: NDArray) -> NDArray:
    """Element-wise sums / counts with 0.0 where counts is 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        means = np.divide(sums, counts)
    return np.nan_to_num(means, nan=0.0)


def compute_compartment_intensities(
    cell_mask: NDArray,
    nuclei_mask: Optional[NDArray],
    channel: NDArray,
    channel_name: str,
    statistics=None,
    redsea: Optional[tuple] = None,
) -> pd.DataFrame:
    """Compute per-cell signal for one channel — Median-first.

    The default per-channel statistic is **Median** and is ALWAYS emitted. With
    ``statistics`` selects which of Median / Mean / Sum / REDSEA are emitted
    (default ``["Median"]``). This statistic policy is independent of compartments:

    - ``nuclei_mask is None``  -> only the whole-cell ``Cell`` compartment.
    - ``nuclei_mask`` supplied  -> additionally ``Nucleus`` and ``Cytoplasm``.

    When a nuclear mask is supplied, the nuclear signal of cell ``C`` is measured
    over the pixels that belong to cell ``C`` **and** to any nucleus, i.e.
    ``(cell_mask == C) & (nuclei_mask > 0)`` — keyed by the *cell* label, so no
    nucleus->cell pairing is required and the masks need not share label IDs.
    Cytoplasm = ``Cell - Nucleus`` by subtraction (non-negative; the nuclear region
    is a subset of the cell). Works for any backend exposing both masks.

    Output columns are the *final QuPath measurement keys*, so they flow unchanged
    through merge_quant_csvs -> export_geojson -> GeoJSON:

    - ``label``
    - ``<channel>``  (== whole-cell **mean**, kept for backward compatibility and as
      FlowPath's bare-key fallback — that fast path is hard-wired to whole-cell mean)
    - ``<channel>: <Compartment>: Median``  (default statistic, always)
    - one ``: <Compartment>: <Statistic>`` per requested compartment statistic
    - ``: Cell: REDSEA`` (whole-cell only) when ``REDSEA`` is requested

    where ``<Compartment>`` is ``Cell`` (always) plus ``Nucleus``/``Cytoplasm`` when
    a nuclear mask is supplied.

    Parameters
    ----------
    cell_mask : ndarray, shape (Y, X)
        Whole-cell instance mask (background = 0).
    nuclei_mask : ndarray or None, shape (Y, X)
        Nuclear instance mask. When None, only the whole-cell compartment is emitted.
    channel : ndarray, shape (Y, X)
        Single-channel intensity image.
    channel_name : str
        Marker name used to build measurement keys.
    statistics : list or str, optional
        Which statistics to emit, from ``measurements.STATISTICS``. Defaults to
        ``["Median"]``. ``REDSEA`` additionally requires ``redsea``.
    redsea : tuple or None
        ``(weights, band, redsea_checker)`` from ``bin/redsea_matrix.py``. When
        given, two extra WHOLE-CELL columns are appended:
        ``"<marker>: Cell: REDSEA Sum"`` and ``"<marker>: Cell: REDSEA Mean"``.

        Only whole-cell, and only Sum/Mean, both on purpose. REDSEA's algebra
        subtracts a fraction of a neighbour's *integrated* boundary counts, so it
        is defined on sums; a median has no such algebra, which is why the
        pipeline's default Median statistic is emitted un-compensated alongside.
        And the method is a whole-cell membrane correction — it has no
        nucleus/cytoplasm decomposition, so it is not emitted per compartment.

    Returns
    -------
    DataFrame
        One row per (whole-cell) label. Empty DataFrame if no cells.
    """
    statistics = resolve_statistics(statistics)
    cell_mask = np.ascontiguousarray(cell_mask.squeeze())
    channel = channel.squeeze()
    has_nuclei = nuclei_mask is not None
    if has_nuclei:
        nuclei_mask = np.ascontiguousarray(nuclei_mask.squeeze())
        if cell_mask.shape != nuclei_mask.shape:
            raise ValueError(
                f"cell/nuclei mask shape mismatch: {cell_mask.shape} vs {nuclei_mask.shape}"
            )
    if cell_mask.shape != channel.shape:
        raise ValueError(
            f"mask/channel shape mismatch: {cell_mask.shape} vs {channel.shape}"
        )

    valid_labels = np.unique(cell_mask)
    valid_labels = valid_labels[valid_labels != 0]
    if len(valid_labels) == 0:
        logger.warning("[WARN] No cells found in cell mask")
        return pd.DataFrame()

    n = int(cell_mask.max()) + 1
    flat_cell = cell_mask.ravel()
    flat_val = channel.ravel().astype(np.float64)

    # Whole-cell sums/counts keyed by cell label.
    cell_sum = np.bincount(flat_cell, weights=flat_val, minlength=n)
    cell_count = np.bincount(flat_cell, minlength=n)
    sums = {"Cell": cell_sum}
    counts = {"Cell": cell_count}

    nuc_fg = None
    if has_nuclei:
        # Nuclear region (cell ∩ nucleus) keyed by *cell* label — label-pairing free.
        nuc_fg = nuclei_mask.ravel() > 0
        nuc_cell_labels = np.where(nuc_fg, flat_cell, 0)
        nuc_sum = np.bincount(nuc_cell_labels, weights=flat_val, minlength=n)
        nuc_count = np.bincount(nuc_cell_labels, minlength=n)
        sums["Nucleus"] = nuc_sum
        counts["Nucleus"] = nuc_count
        # Cytoplasm = whole-cell minus nuclear region (per cell, by subtraction).
        sums["Cytoplasm"] = cell_sum - nuc_sum
        counts["Cytoplasm"] = cell_count - nuc_count

    # Canonical order, restricted to the compartments actually computed.
    compartments = [c for c in COMPARTMENT_NAMES if c in sums]

    out: dict = {"label": valid_labels}
    # Backward-compatible bare marker column (== whole-cell mean). FlowPath's
    # bare-key fast path is hard-wired to (WHOLE_CELL, Mean), so this stays mean;
    # the Median default reaches FlowPath via the structured "<marker>: Cell: Median"
    # key below plus FlowPath defaulting its statistic selector to Median.
    out[channel_name] = _safe_mean(cell_sum, cell_count)[valid_labels]

    # Compute each BASE statistic at most once, then emit whichever composed names
    # were asked for. A z-scored column is the base column standardised across this
    # patient's cells, so asking for "Mean Z" without "Mean" still only computes the
    # mean once and simply does not emit the raw column.
    needed_bases = {split_statistic(st)[0] for st in statistics}
    base_values: dict = {}

    if "Median" in needed_bases:
        # scipy.ndimage.labeled_comprehension directly on the label/value arrays
        # already built above for the bincount sums -- NOT a per-pixel DataFrame.
        # A per-pixel pandas DataFrame (one row per foreground pixel, rebuilt on
        # every channel) is what caused the WSI-scale memory regression;
        # labeled_comprehension computes the same per-label median in bounded
        # memory. A label absent from the underlying label array (e.g. a cell with
        # no nuclear overlap) yields `default`, reproducing the old
        # `.reindex(valid_labels).fillna(0.0)` behaviour exactly (verified against
        # the old groupby-median path in tests/test_quantify_median.py).
        base_values[("Median", "Cell")] = ndimage.labeled_comprehension(
            flat_val, flat_cell, valid_labels, np.median, np.float64, 0.0
        )
        if has_nuclei:
            cyto_cell_labels = np.where(nuc_fg, 0, flat_cell)
            base_values[("Median", "Nucleus")] = ndimage.labeled_comprehension(
                flat_val, nuc_cell_labels, valid_labels, np.median, np.float64, 0.0
            )
            base_values[("Median", "Cytoplasm")] = ndimage.labeled_comprehension(
                flat_val, cyto_cell_labels, valid_labels, np.median, np.float64, 0.0
            )

    if "Mean" in needed_bases:
        for comp in compartments:
            base_values[("Mean", comp)] = _safe_mean(sums[comp], counts[comp])[
                valid_labels
            ]

    if "Sum" in needed_bases:
        for comp in compartments:
            base_values[("Sum", comp)] = sums[comp][valid_labels]

    if REDSEA_STATISTIC in needed_bases and redsea is not None:
        weights, band, redsea_checker = redsea
        if band.shape != cell_mask.shape:
            raise ValueError(
                f"REDSEA band/mask shape mismatch: {band.shape} vs {cell_mask.shape}. "
                "The geometry was built from a different segmentation than the one "
                "being quantified."
            )
        if weights.shape[0] < n:
            raise ValueError(
                f"REDSEA geometry covers {weights.shape[0] - 1} labels but the mask "
                f"has {n - 1}. Rebuild the geometry from this mask."
            )
        # `cell_sum` is already the whole-cell sum; band_sums recomputes it so the
        # two vectors are guaranteed to share a length and label indexing with the
        # geometry (which is sized from the mask that built it, not this channel).
        whole, edge = band_sums(cell_mask, band, channel, n_labels=weights.shape[0])
        comp_sum = compensate(whole, edge, weights, redsea_checker=redsea_checker)
        area = np.bincount(flat_cell, minlength=weights.shape[0])[: weights.shape[0]]
        # WHOLE-CELL ONLY, and the SIZE-NORMALISED compensated value -- the
        # reference implementation's own recommended output.
        base_values[(REDSEA_STATISTIC, "Cell")] = _safe_mean(comp_sum, area)[
            valid_labels
        ]

    # Emit in the canonical vocabulary order, grouped by base statistic so a
    # marker's raw / Z / RobustZ columns stay adjacent.
    for base in BASE_STATISTICS:
        comps = ["Cell"] if base == REDSEA_STATISTIC else compartments
        for norm in NORMALIZATIONS:
            name = base + norm
            if name not in statistics:
                continue
            for comp in comps:
                values = base_values.get((base, comp))
                if values is None:
                    continue
                if norm:
                    # Standardised across THIS PATIENT's cells for this marker and
                    # compartment -- the population a QUANTIFY task already holds.
                    values = zscore(values, robust=(norm == " RobustZ"))
                out[f"{channel_name}: {comp}: {name}"] = values


    return pd.DataFrame(out)


def quantify_single_channel(
    mask: NDArray,
    channel_image: NDArray,
    channel_name: str,
    nuclei_mask: Optional[NDArray] = None,
    statistics=None,
    redsea: Optional[tuple] = None,
) -> pd.DataFrame:
    """Quantify a single channel (intensity only).

    Morphology is computed separately by EXTRACT_CELL_PROPERTIES.

    Delegates to :func:`compute_compartment_intensities`, which is Median-first:
    ``statistics`` chooses the statistics and ``nuclei_mask`` only controls whether
    the Nucleus/Cytoplasm compartments are added alongside the whole-cell ``Cell``
    compartment (so the statistic set is the same with or without a nuclear mask).

    Parameters
    ----------
    mask : ndarray, shape (Y, X)
        Whole-cell segmentation mask.
    channel_image : ndarray, shape (Y, X)
        Channel intensity image.
    channel_name : str
        Name of the channel/marker.
    nuclei_mask : ndarray, optional
        Nuclear segmentation mask. If given, adds Nucleus/Cytoplasm compartments.
    statistics : list or str, optional
        Which statistics to emit (default ``["Median"]``).

    Returns
    -------
    DataFrame
        Intensity per cell: ``label``, bare ``{channel_name}`` (whole-cell mean),
        and one per-compartment measurement key per requested statistic.
    """
    mask = np.ascontiguousarray(mask.squeeze())
    return compute_compartment_intensities(
        mask, nuclei_mask, channel_image, channel_name, statistics=statistics, redsea=redsea
    )


def _load_mask(mask_path: str) -> NDArray:
    """Load a segmentation mask from .npy or .tif, squeezed to 2D."""
    if mask_path.endswith(".npy"):
        return np.load(mask_path).squeeze()
    loaded, _ = load_image(mask_path)
    return loaded.squeeze()


def run_quantification(
    mask_path: str,
    channel_path: str,
    output_path: str,
    channel_name: str = None,
    nuclei_mask_path: Optional[str] = None,
    statistics=None,
    redsea_geometry_path: Optional[str] = None,
    redsea_markers=None,
    redsea_checker: int = 1,
) -> pd.DataFrame:
    """Run quantification for a single channel.

    Parameters
    ----------
    mask_path : str
        Path to whole-cell segmentation mask (.npy or .tif file).
    channel_path : str
        Path to channel image file (.tif).
    output_path : str
        Path to save output CSV file.
    channel_name : str, optional
        Explicit channel/marker name. If not provided, will be parsed
        from the channel file basename.
    nuclei_mask_path : str, optional
        Path to the nuclear mask. If provided, per-compartment quantification
        (Nucleus / Cytoplasm / Cell) is performed instead of whole-cell only.
    statistics : list or str, optional
        Which statistics to emit (default ``["Median"]``).
    redsea_geometry_path : str, optional
        ``<patient>_redsea.npz`` from ``bin/redsea_matrix.py``. Present on every
        marker task of a REDSEA-enabled run; whether it is actually *used* is
        decided per marker by ``redsea_markers``.
    redsea_markers : list or str, optional
        Markers that opt in to compensation. A channel not in this list is
        quantified exactly as before, so turning REDSEA on never silently changes
        an existing column.
    redsea_checker : int
        1 = subtract and reinforce (the paper's default), 0 = subtract only.

    Returns
    -------
    pd.DataFrame
        Quantification results with columns: label, {channel_name}.
        Morphology is computed separately by EXTRACT_CELL_PROPERTIES.

    Raises
    ------
    FileNotFoundError
        If mask_path or channel_path does not exist.
    ValueError
        If mask and channel have incompatible shapes.
    """
    # Resolve ONCE here so the log line, the REDSEA gate and the empty-CSV column
    # parity all read the same list; compute_compartment_intensities re-resolves
    # its own argument, which is idempotent.
    resolved_statistics = resolve_statistics(statistics)

    # Use provided channel name, or extract from filename as fallback
    if channel_name is None:
        channel_name = Path(channel_path).stem.split("_")[-1]
        logger.info(f"Channel name parsed from filename: {channel_name}")

    logger.info("=" * 60)
    logger.info(f"Quantifying channel: {channel_name}")
    logger.info("=" * 60)

    # Load segmentation mask
    logger.info(f"Loading mask: {mask_path}")
    try:
        if mask_path.endswith(".npy"):
            mask = np.load(mask_path).squeeze()
        else:
            mask, _ = load_image(mask_path)
            mask = mask.squeeze()
        logger.debug(f"  Mask shape: {mask.shape}")
    except FileNotFoundError:
        logger.error(f"[FAIL] Segmentation mask not found: {mask_path}")
        raise FileNotFoundError(f"Segmentation mask not found: {mask_path}")
    except Exception as e:
        logger.error(f"[FAIL] Failed to load mask from {mask_path}: {e}")
        raise ValueError(f"Failed to load mask from {mask_path}: {e}") from e

    # Load channel image
    logger.info(f"Loading channel: {channel_path}")
    try:
        channel_image, _ = load_image(channel_path)
        logger.debug(f"  Channel shape: {channel_image.shape}")
    except FileNotFoundError:
        logger.error(f"[FAIL] Channel image not found: {channel_path}")
        raise FileNotFoundError(f"Channel image not found: {channel_path}")
    except Exception as e:
        logger.error(f"[FAIL] Failed to load channel from {channel_path}: {e}")
        raise ValueError(f"Failed to load channel from {channel_path}: {e}") from e

    # Validate shapes match
    if mask.shape != channel_image.squeeze().shape:
        logger.error(
            f"[FAIL] Shape mismatch: mask {mask.shape} vs channel {channel_image.squeeze().shape}"
        )
        raise ValueError(
            f"Shape mismatch: mask {mask.shape} vs channel {channel_image.squeeze().shape}. "
            f"Ensure the mask and channel images have the same spatial dimensions."
        )

    # Optionally load the nuclear mask for per-compartment quantification
    nuclei_mask = None
    if nuclei_mask_path:
        logger.info(f"Loading nuclei mask: {nuclei_mask_path}")
        try:
            nuclei_mask = _load_mask(nuclei_mask_path)
        except FileNotFoundError:
            logger.error(f"[FAIL] Nuclei mask not found: {nuclei_mask_path}")
            raise FileNotFoundError(f"Nuclei mask not found: {nuclei_mask_path}")
        except Exception as e:
            logger.error(
                f"[FAIL] Failed to load nuclei mask from {nuclei_mask_path}: {e}"
            )
            raise ValueError(
                f"Failed to load nuclei mask from {nuclei_mask_path}: {e}"
            ) from e
        logger.info(
            "Per-compartment quantification enabled: Nucleus/Cytoplasm/Cell, "
            f"{', '.join(resolved_statistics)}"
        )

    # Optionally load the REDSEA geometry. Two independent gates, both cheap and
    # both needed: the run must have a geometry at all, and THIS marker must be one
    # the user opted in (REDSEA is only valid for membrane markers -- see
    # is_redsea_marker). A non-opted marker never even opens the npz.
    # THREE independent gates, all cheap: REDSEA must be in the requested
    # statistics, the run must have a geometry, and THIS marker must be one the
    # user opted in (REDSEA is only valid for membrane markers -- see
    # is_redsea_marker). Any one missing and the npz is never even opened.
    redsea = None
    want_redsea = REDSEA_STATISTIC in resolved_statistics
    if want_redsea and redsea_geometry_path and is_redsea_marker(
        channel_name, redsea_markers
    ):
        sys.path.insert(0, str(Path(__file__).parent))
        from redsea_matrix import load_geometry

        logger.info(f"REDSEA: loading geometry {redsea_geometry_path}")
        weights, band, _labels = load_geometry(redsea_geometry_path)
        redsea = (weights, band, redsea_checker)
        logger.info(
            f"REDSEA: compensating '{channel_name}' "
            f"(REDSEAChecker={redsea_checker}, {weights.nnz} cell-pair weights)"
        )
    elif want_redsea and redsea_geometry_path:
        logger.info(
            f"REDSEA: '{channel_name}' is not in --redsea-markers, quantifying uncompensated"
        )

    # Quantify
    result_df = quantify_single_channel(
        mask,
        channel_image,
        channel_name,
        nuclei_mask=nuclei_mask,
        statistics=resolved_statistics,
        redsea=redsea,
    )

    # Save
    if not result_df.empty:
        result_df.to_csv(output_path, index=False)
        logger.info(f"[OK] Saved {len(result_df)} cells to {output_path}")
    else:
        logger.warning("[WARN] No results to save")
        # Empty CSV with the SAME columns a non-empty result would have, so the
        # downstream per-channel merge stays consistent when a channel's mask
        # contains no cells. Mirrors compute_compartment_intensities: bare column,
        # one per-compartment key per requested compartment statistic, then the
        # whole-cell-only REDSEA key. Compartments = Cell only without a nuclear
        # mask, else Nucleus/Cytoplasm/Cell.
        comps = list(COMPARTMENT_NAMES) if nuclei_mask is not None else ["Cell"]
        cols = ["label", channel_name]
        # Same nesting as compute_compartment_intensities' emission loop, so an
        # all-empty channel's header matches a populated one exactly.
        for base in BASE_STATISTICS:
            if base == REDSEA_STATISTIC and redsea is None:
                continue
            base_comps = ["Cell"] if base == REDSEA_STATISTIC else comps
            for norm in NORMALIZATIONS:
                name = base + norm
                if name in resolved_statistics:
                    cols += [f"{channel_name}: {c}: {name}" for c in base_comps]
        empty_df = pd.DataFrame(columns=cols)
        empty_df.to_csv(output_path, index=False)

    logger.info("Quantification complete")

    return result_df


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Single-channel cell quantification (Nextflow compatible)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--channel_tiff",
        type=str,
        required=True,
        help="Path to single channel TIFF image",
    )
    parser.add_argument(
        "--mask_file",
        type=str,
        required=True,
        help="Path to whole-cell segmentation mask (.npy or .tif)",
    )
    parser.add_argument(
        "--nuclei_mask_file",
        type=str,
        default=None,
        help="Path to nuclear mask (.npy or .tif). If given, enables per-compartment "
        "quantification (Nucleus/Cytoplasm/Cell).",
    )
    parser.add_argument(
        "--statistics",
        type=str,
        default=None,
        help="Comma-separated statistics to emit, from "
        f"{{{', '.join(STATISTICS)}}}. Default: {','.join(DEFAULT_STATISTICS)}. "
        "Median/Mean/Sum are emitted per compartment; REDSEA is whole-cell only "
        "and additionally requires --redsea-geometry and --redsea-markers.",
    )
    parser.add_argument("--outdir", type=str, default=".", help="Output directory")
    parser.add_argument(
        "--output_file",
        type=str,
        help="Output CSV filename (default: {channel}_quant.csv)",
    )
    parser.add_argument(
        "--channel-name",
        type=str,
        default=None,
        help="Explicit channel name (if not provided, will parse from filename)",
    )
    parser.add_argument(
        "--redsea-geometry",
        type=str,
        default=None,
        help="Per-patient REDSEA geometry (.npz) from redsea_matrix.py. Adds "
        "'<marker>: Cell: REDSEA Sum' and '... REDSEA Mean' for markers listed in "
        "--redsea-markers; every other column is unchanged.",
    )
    parser.add_argument(
        "--redsea-markers",
        type=str,
        default=None,
        help="Comma-separated markers to compensate. Surface/membrane markers only "
        "-- REDSEA assumes the signal sits on the membrane and is brighter in the "
        "source cell than in the leak.",
    )
    parser.add_argument(
        "--redsea-checker",
        type=int,
        default=1,
        choices=[0, 1],
        help="REDSEAChecker: 1 = subtract and reinforce (paper default), "
        "0 = subtract only.",
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    configure_logging(level=logging.INFO)

    args = parse_args()

    # Ensure output directory exists
    ensure_dir(args.outdir)

    # Derive effective channel name once for consistency
    effective_channel_name = (
        args.channel_name or Path(args.channel_tiff).stem.split("_")[-1]
    )

    # Determine output path
    if args.output_file:
        output_path = os.path.join(args.outdir, args.output_file)
    else:
        output_path = os.path.join(args.outdir, f"{effective_channel_name}_quant.csv")

    # Run quantification (CPU mode)
    # Note: GPU mode removed - use GPU container if GPU acceleration needed
    logger.info("Running CPU quantification")
    run_quantification(
        mask_path=args.mask_file,
        channel_path=args.channel_tiff,
        output_path=output_path,
        channel_name=effective_channel_name,
        nuclei_mask_path=args.nuclei_mask_file,
        statistics=args.statistics,
        redsea_geometry_path=args.redsea_geometry,
        redsea_markers=args.redsea_markers,
        redsea_checker=args.redsea_checker,
    )

    return 0


if __name__ == "__main__":
    exit(main())
