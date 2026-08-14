#!/usr/bin/env python3
"""Per-patient REDSEA geometry: the channel-independent half of the compensation.

REDSEA (Bai et al., Front. Immunol. 2021) splits cleanly in two:

  * a per-patient part that depends only on the segmentation mask -- which cells
    touch which, how much perimeter they share, and which pixels lie in each
    cell's boundary band -- computed here, ONCE;
  * a per-channel part that is one sparse mat-vec, applied inside QUANTIFY, in
    parallel across every marker.

Keeping the split explicit is what makes REDSEA cheap in this pipeline: the mask
pass is not repeated N_markers times, and the per-marker tasks stay independent.

Emits
-----
``<prefix>_redsea.npz``
    ``weights`` (CSR triple) -- shared-perimeter fractions, label-indexed;
    ``band`` (bit-packed) + ``band_shape`` -- the boundary band raster;
    ``labels`` -- the mask's non-zero labels.
``<prefix>_redsea_qc.json``
    Band-fraction statistics and the parameters used. Read this before trusting a
    run: the band fraction is the parameter-sanity signal (see --element-size).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from image_utils import ensure_dir, load_image  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from redsea import (  # noqa: E402
    ELEMENT_SHAPES,
    calibrate_element_size,
    recommend_element_size,
    redsea_geometry,
)

logger = get_logger(__name__)

__all__ = ["build_geometry", "load_geometry", "save_geometry"]


def _load_mask(mask_path: str) -> np.ndarray:
    """Load a segmentation mask from .npy or .tif, squeezed to 2D.

    Same two-format contract as ``quantify.py::_load_mask`` -- SEGMENT's backends
    differ in which they write.
    """
    if str(mask_path).endswith(".npy"):
        return np.load(mask_path).squeeze()
    loaded, _ = load_image(str(mask_path))
    return loaded.squeeze()


def save_geometry(geom, out_npz: Path) -> None:
    """Write the geometry bundle.

    The band is stored bit-packed (``np.packbits``): it is a full-raster boolean,
    so at WSI scale the naive form is one byte per pixel staged into every one of
    the patient's per-marker tasks. Packed it is an eighth of that before
    compression, and unpacking is a single vectorised call.
    """
    w = geom.weights.tocsr()
    np.savez_compressed(
        out_npz,
        w_data=w.data.astype(np.float32),
        w_indices=w.indices.astype(np.int32),
        w_indptr=w.indptr.astype(np.int64),
        w_shape=np.asarray(w.shape, dtype=np.int64),
        band=np.packbits(geom.band.ravel()),
        band_shape=np.asarray(geom.band.shape, dtype=np.int64),
        labels=geom.labels.astype(np.int64),
    )


def load_geometry(npz_path: str):
    """Read back what :func:`save_geometry` wrote.

    Returns ``(weights_csr, band_bool_2d, labels)``. Imported by
    ``bin/quantify.py``; this module is not a library-only file (see
    ``tests/test_no_dead_bin_modules.py`` for why that matters here).
    """
    import scipy.sparse as sp

    z = np.load(npz_path)
    shape = tuple(int(v) for v in z["w_shape"])
    weights = sp.csr_matrix(
        (z["w_data"].astype(np.float64), z["w_indices"], z["w_indptr"]), shape=shape
    )
    band_shape = tuple(int(v) for v in z["band_shape"])
    n = int(np.prod(band_shape))
    band = np.unpackbits(z["band"])[:n].astype(bool).reshape(band_shape)
    return weights, band, z["labels"]


def build_geometry(
    mask_path: str,
    outdir: str,
    prefix: str,
    element_size: int | None,
    element_shape: str,
    pixel_size_um: float | None = None,
    cell_diameter_um: float | None = None,
    target_band_fraction: float | None = None,
) -> dict:
    """Compute, write and QC one patient's REDSEA geometry.

    ``element_size=None`` means calibrate it from this mask against
    ``target_band_fraction`` -- the recommended way to set it (see
    ``redsea.calibrate_element_size`` for why the closed-form rule over-shoots).
    """
    outdir_p = ensure_dir(outdir)
    mask = _load_mask(mask_path)
    logger.info(f"Mask {mask_path}: shape={mask.shape} dtype={mask.dtype}")

    calibration = None
    if element_size is None:
        target = target_band_fraction if target_band_fraction else 0.56
        element_size, calibration = calibrate_element_size(
            mask, target_fraction=target, element_shape=element_shape
        )
        logger.info(
            f"Calibrated --element-size {element_size} px "
            f"(band fraction {calibration[element_size]:.3f}, target {target:.2f})"
        )

    geom = redsea_geometry(mask, element_size=element_size, element_shape=element_shape)
    n_cells = int(geom.labels.size)
    logger.info(
        f"{n_cells} cells; {geom.n_neighbourless} with no touching neighbour "
        f"({100.0 * geom.n_neighbourless / max(n_cells, 1):.1f}%)"
    )

    save_geometry(geom, outdir_p / f"{prefix}_redsea.npz")

    frac = geom.band_fraction
    qc = {
        "n_cells": n_cells,
        "n_neighbourless": geom.n_neighbourless,
        "neighbourless_fraction": float(geom.n_neighbourless / n_cells) if n_cells else 0.0,
        "element_size": element_size,
        "element_shape": element_shape,
        "band_fraction_mean": float(frac.mean()) if n_cells else 0.0,
        "band_fraction_median": float(np.median(frac)) if n_cells else 0.0,
        "band_fraction_p90": float(np.percentile(frac, 90)) if n_cells else 0.0,
        "cells_fully_band": int((frac >= 0.999).sum()),
        "mean_cell_area_px": float(
            np.bincount(mask.ravel())[geom.labels].mean()
        ) if n_cells else 0.0,
    }
    if calibration is not None:
        qc["element_size_calibrated"] = True
        qc["band_fraction_by_element_size"] = {
            str(k): round(v, 4) for k, v in calibration.items() if k <= 40
        }
    if pixel_size_um and cell_diameter_um:
        qc["recommended_element_size"] = recommend_element_size(
            cell_diameter_um, pixel_size_um
        )
        qc["pixel_size_um"] = pixel_size_um
        qc["cell_diameter_um"] = cell_diameter_um

    # THE parameter-sanity signal. The REDSEApy port's author documents seeing
    # "cells that looked like they were ALL border pixels -- which kind of ruins
    # the point"; at that point boundary mode has silently degraded into
    # whole-cell mode and no error is raised anywhere. Warn loudly instead.
    if qc["band_fraction_mean"] > 0.85:
        logger.warning(
            f"[WARN] mean band fraction {qc['band_fraction_mean']:.2f} -- the boundary "
            f"band is nearly the whole cell at --element-size {element_size}. REDSEA's "
            "boundary mode has effectively become whole-cell mode. Reduce --element-size."
        )
    elif qc["band_fraction_mean"] < 0.15:
        logger.warning(
            f"[WARN] mean band fraction {qc['band_fraction_mean']:.2f} -- the band is a "
            f"thin sliver at --element-size {element_size}, so very little membrane "
            "signal is being compensated. The published settings sit near 0.56."
        )
    else:
        logger.info(
            f"Band fraction mean={qc['band_fraction_mean']:.2f} "
            f"(the published MIBI/CyCIF settings sit near 0.56)"
        )

    with open(outdir_p / f"{prefix}_redsea_qc.json", "w") as fh:
        json.dump(qc, fh, indent=2)
    return qc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the per-patient REDSEA compensation geometry."
    )
    parser.add_argument("--mask_file", required=True, help="Whole-cell mask (.npy/.tif)")
    parser.add_argument("--outdir", default=".", help="Output directory")
    parser.add_argument("--prefix", required=True, help="Output filename prefix")
    parser.add_argument(
        "--element-size",
        type=int,
        default=None,
        help=(
            "Boundary band depth in PIXELS. Omit to calibrate it from the mask "
            "against --target-band-fraction, which is the recommended way to set "
            "it: the paper's MIBI (2 px) and CyCIF (3-4 px) settings both sit at "
            "~17%% of the cell diameter, but that closed-form rule assumes disks "
            "and over-shoots on real cell shapes."
        ),
    )
    parser.add_argument(
        "--target-band-fraction",
        type=float,
        default=0.56,
        help="Calibration target when --element-size is omitted. 0.56 is where both "
        "of the paper's own calibration points land.",
    )
    parser.add_argument(
        "--element-shape",
        default="disk",
        choices=sorted(ELEMENT_SHAPES),
        help="Band metric. disk=Euclidean (default), square/diamond reproduce the "
        "original's strel('square')/strel('diamond').",
    )
    parser.add_argument(
        "--pixel-size",
        dest="pixel_size_um",
        type=float,
        default=None,
        help="Pixel size, for the recommended-element-size line in the QC JSON only.",
    )
    parser.add_argument(
        "--cell-diameter-um",
        type=float,
        default=None,
        help="Typical cell diameter, for the recommended-element-size QC line only.",
    )
    args = parser.parse_args(argv)

    # Repo convention (bin/quantify.py:437, bin/segment.py:582): a fixed level,
    # not a CLI knob -- Nextflow controls verbosity through its own logging.
    configure_logging(level=logging.INFO)
    build_geometry(
        mask_path=args.mask_file,
        outdir=args.outdir,
        prefix=args.prefix,
        element_size=args.element_size,
        element_shape=args.element_shape,
        pixel_size_um=args.pixel_size_um,
        cell_diameter_um=args.cell_diameter_um,
        target_band_fraction=args.target_band_fraction,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
