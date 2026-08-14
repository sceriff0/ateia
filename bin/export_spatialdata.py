#!/usr/bin/env python3
"""Serialize MIRAGE postprocessing output into a SpatialData ``.zarr`` store.

Nothing here is computed that MIRAGE does not already produce — this is a
serializer. Four of SpatialData's five element types map onto existing artifacts:

===========  ==========================================================
element      MIRAGE artifact
===========  ==========================================================
Image        ``pyramid.ome.tiff``            (optional, see --include-image)
Labels       ``*_cell_mask.tif`` / ``*_nuclei_mask.tif``
Shapes       ``contours.json`` (+ nucleus contours)
Table        merged quantification CSV (intensities + morphology)
Points       n/a — protein imaging, no transcripts
===========  ==========================================================

Two entry points, one script:

* **pipeline mode** (default) builds the store from postprocessing artifacts.
* **attach mode** (``--attach-phenotypes``) reopens an existing store and adds
  FlowPath gating results. Phenotyping happens interactively in QuPath *after*
  the pipeline finishes, so it can never be a DAG dependency.

The table's ``instance_key`` is the segmentation ``label``. That column is the
only stable cell identifier in the pipeline, so every join here is keyed on it
and mismatches are fatal rather than silently positional.
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

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from logger import configure_logging, get_logger  # noqa: E402
from measurements import (  # noqa: E402
    COMPARTMENTS,
    MORPHOLOGY_COLS,
    STATISTICS,
    identify_marker_columns,
)

logger = get_logger(__name__)

REGION_LABELS = "cell_mask"
INSTANCE_KEY = "label"
REGION_KEY = "region"


# ── measurement keys ───────────────────────────────────────────────────────────
def parse_measurement_key(key: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Split ``"CD3: Nucleus: Median"`` into ``("CD3", "Nucleus", "Median")``.

    A bare marker name (legacy / whole-cell-mean export) returns
    ``(key, None, None)`` rather than guessing, so ``var`` never asserts a
    compartment the pipeline did not actually measure.

    Matching is on the *last two* tokens, because marker names may themselves
    contain ": " (e.g. an antibody clone).
    """
    for comp in COMPARTMENTS:
        for stat in STATISTICS:
            suffix = f": {comp}: {stat}"
            if key.endswith(suffix):
                marker = key[: -len(suffix)].strip()
                if marker:
                    return marker, comp, stat
    return key, None, None


# ── QC sanitizing ──────────────────────────────────────────────────────────────
def sanitize_for_uns(obj):
    """Make a QC document safe to write through AnnData → Zarr.

    Python ``None`` does not round-trip cleanly; ``bin/warp_seg_qc.py`` emits
    ``"radius_factor": None`` today. Numeric ``None`` becomes NaN, everything
    else is dropped. Also coerces numpy scalars, which AnnData writes as
    0-d arrays that read back awkwardly.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            s = sanitize_for_uns(v)
            if s is not None:
                out[str(k)] = s
        return out
    if isinstance(obj, (list, tuple)):
        cleaned = [sanitize_for_uns(v) for v in obj]
        return [v for v in cleaned if v is not None]
    if obj is None:
        return float("nan")
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def check_patient(found, seen: set, patient_id: Optional[str], source: str) -> None:
    """Refuse QC that belongs to a patient other than the one being exported.

    One store is one patient. Every reg_qc=2 artifact reports coordinates and
    metrics in that patient's own reference frame — its own pixel grid, rooted at
    (0, 0) like every other patient's — so an artifact from a different patient is
    not noise to be filtered out but a measurement in the wrong coordinate system.
    Joining it produces a confident wrong answer; dropping it silently produces a
    store that looks complete while a caller's expectation about what reached it
    was wrong. Both are worse than stopping.

    Two rules, so this holds whether or not the caller knows the patient:

    * ``patient_id`` given (the pipeline always gives it, from ``--patient-id``):
      anything naming a different patient is rejected outright.
    * ``patient_id`` unknown: a *set* of artifacts naming more than one patient is
      rejected, because whatever the answer is, it cannot be all of them.

    ``found`` falsy means the artifact does not state a patient at all — a QC doc
    or residual CSV written before the field existed. Those are accepted: the
    per-patient channel join in ``subworkflows/local/postprocess.nf`` is what
    guarantees only this patient's files arrive, and this check is defence in
    depth behind it, not a reason to refuse data that predates it.
    """
    if not found:
        return
    # Compared as stripped strings: the samplesheet reader strips patient_id (see
    # tests/testdata/whitespace_patient_id.csv), so " P001" and "P001" are the same
    # patient everywhere else in the pipeline and must be here too.
    found = str(found).strip()
    expected = None if patient_id is None else str(patient_id).strip()
    if not found:
        return
    if expected is not None and found != expected:
        raise ValueError(
            f"{source} carries registration QC for patient {found!r}, but this export "
            f"is for patient {expected!r}. Every QC coordinate is in its own "
            "patient's reference frame, so joining it here would silently attach one "
            "patient's registration evidence to another's cells."
        )
    seen.add(found)
    if len(seen) > 1:
        raise ValueError(
            f"registration QC for more than one patient reached a single export "
            f"({', '.join(sorted(seen))}); {source} is the artifact that made it "
            "ambiguous. One .zarr is one patient: these artifacts must be joined by "
            "patient_id, not collected run-wide."
        )


def load_qc(
    reg_qc_paths: List[str],
    versions_paths: List[str],
    patient_id: Optional[str] = None,
) -> Tuple[Dict, Dict]:
    """Collect QC into a flattened dict (``uns['qc']``) and verbatim JSON strings.

    Storing both is deliberate: the flattened form is what people actually index
    into, and the verbatim string guarantees nothing is lost to flattening.

    Keys are moving-slide names, which are unique WITHIN a patient and nothing
    more — so the docs are checked against ``patient_id`` (which every doc carries;
    see ``bin/warp_seg_qc.py``'s ``build_record``) before they are keyed. See
    ``check_patient``.
    """
    qc: Dict = {"registration": {}}
    raw: Dict = {"registration": {}}
    seen: set = set()

    for p in reg_qc_paths:
        try:
            doc = json.loads(Path(p).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping unreadable registration QC %s: %s", p, exc)
            continue
        check_patient(doc.get("patient_id"), seen, patient_id, Path(p).name)
        key = str(doc.get("moving") or Path(p).stem)
        qc["registration"][key] = sanitize_for_uns(doc)
        raw["registration"][key] = json.dumps(doc)

    versions: Dict[str, str] = {}
    for p in versions_paths:
        try:
            versions[Path(p).name] = Path(p).read_text()
        except OSError as exc:
            logger.warning("skipping unreadable versions file %s: %s", p, exc)
    return qc, {"qc_json": raw, "versions": versions}


# ── per-cell registration residuals ────────────────────────────────────────────
def join_reg_residuals(
    residual_paths: List[str],
    centroids_xy: np.ndarray,
    labels: np.ndarray,
    max_dist_px: float,
    patient_id: Optional[str] = None,
) -> Tuple[pd.DataFrame, Dict]:
    """Spatially join per-cell registration residuals onto segmentation labels.

    ``warp_seg_qc`` scores a *separate* StarDist run on each slide's native image
    (see ``modules/local/seg_qc_geojson.nf``), so its cells share **no label
    space** with ``cell_mask``. The two do share a coordinate frame though — the
    residual CSV reports reference centroids in the registered reference frame,
    which is the frame ``SEGMENT`` ran on — so the join is spatial: each QC pair
    is assigned to the nearest ``cell_mask`` centroid within ``max_dist_px``.

    Returns ``(residuals, stats)`` where ``residuals`` is a cells x moving-slides
    frame of displacement in pixels (NaN where a cell was never matched) and
    ``stats`` records how well the join went — an unmatched cell means "no QC
    evidence", NOT "well registered", and conflating those would invert the
    signal's meaning.

    ``ref_x``/``ref_y`` are raw pixels in **this** patient's reference frame, and
    every patient's frame is its own grid rooted at (0, 0) — so another patient's
    rows would land on whatever cell happens to sit within ``max_dist_px`` and be
    written out as a residual column with a plausible ``join_fraction``. Each CSV's
    ``patient_id`` column is therefore checked before its rows are joined; see
    ``check_patient``.
    """
    from scipy.spatial import cKDTree

    out = pd.DataFrame(index=pd.Index(labels, name=INSTANCE_KEY))
    stats: Dict = {"max_dist_px": float(max_dist_px), "slides": {}}
    if not residual_paths or centroids_xy.size == 0:
        return out, stats

    seen: set = set()
    tree = cKDTree(centroids_xy)
    for p in residual_paths:
        try:
            # dtype={"patient_id": str}: pandas infers an all-numeric column as int64,
            # so patient "007" reads back as 7 and str(7) != "007" -- the export would
            # reject the patient's OWN residuals as foreign. Worse in the other
            # direction: "007" and "0007" both become 7, so a genuinely foreign
            # patient's rows would pass the check. Nothing constrains patient_id to be
            # non-numeric (the samplesheet column is free text; there is no
            # assets/schema_input.json), so both are reachable. A dtype key for a
            # column the file does not have is ignored, so this is safe on the legacy
            # header too.
            df = pd.read_csv(p, dtype={"patient_id": str})
        except (OSError, pd.errors.EmptyDataError) as exc:
            logger.warning("skipping unreadable residual CSV %s: %s", p, exc)
            continue
        if df.empty or not {"ref_x", "ref_y", "residual_px"} <= set(df.columns):
            logger.warning("residual CSV %s has no usable rows; skipping", p)
            continue
        if "patient_id" in df.columns:
            for pid in df["patient_id"].dropna().unique():
                check_patient(pid, seen, patient_id, Path(p).name)
        else:
            logger.warning(
                "residual CSV %s predates the patient_id column; its rows cannot be "
                "checked against patient %s and are trusted to the channel that "
                "staged them",
                p,
                patient_id,
            )

        moving = str(df["moving"].iloc[0]) if "moving" in df.columns else Path(p).stem
        query = df[["ref_x", "ref_y"]].to_numpy(dtype=float)
        dist, idx = tree.query(query, distance_upper_bound=float(max_dist_px))
        ok = np.isfinite(dist)

        col = np.full(len(labels), np.nan, dtype=float)
        # Several QC cells can land on one mask cell (different segmentations);
        # keep the worst residual, since this column is used to *exclude* cells and
        # the optimistic choice would hide exactly the cells it exists to flag.
        for cell_i, resid in zip(idx[ok], df["residual_px"].to_numpy(dtype=float)[ok]):
            if np.isnan(col[cell_i]) or resid > col[cell_i]:
                col[cell_i] = resid
        out[moving] = col

        stats["slides"][moving] = {
            "qc_pairs": int(len(df)),
            "joined": int(ok.sum()),
            "join_fraction": float(ok.sum()) / max(len(df), 1),
            "cells_with_residual": int(np.isfinite(col).sum()),
            "cell_coverage": float(np.isfinite(col).sum()) / max(len(labels), 1),
        }
        logger.info(
            "  %s: %d/%d QC pairs joined, %.1f%% of cells covered",
            moving,
            ok.sum(),
            len(df),
            100 * stats["slides"][moving]["cell_coverage"],
        )
    return out, stats


# ── element builders ───────────────────────────────────────────────────────────
def build_shapes(contours_path: str, labels: np.ndarray, name: str):
    """Polygon GeoDataFrame from a ``{label: [[x, y], ...]}`` contours JSON.

    Indexed by ``label`` so it lines up with the table's ``instance_key``. Cells
    without a contour are omitted rather than given a placeholder geometry.
    """
    import geopandas as gpd
    from shapely.geometry import Polygon

    contours = json.loads(Path(contours_path).read_text())
    geoms, idx = [], []
    for lab in labels:
        ring = contours.get(str(int(lab)))
        if not ring or len(ring) < 3:
            continue
        geoms.append(Polygon(ring))
        idx.append(int(lab))
    if not geoms:
        logger.warning("no usable contours in %s for element %r", contours_path, name)
        return None
    logger.info("  shapes/%s: %d polygons (of %d cells)", name, len(geoms), len(labels))
    return gpd.GeoDataFrame({"geometry": geoms}, index=pd.Index(idx, name=INSTANCE_KEY))


def read_mask(path: str) -> np.ndarray:
    """Read a label mask as 2-D integers, squeezing any singleton leading axis."""
    import tifffile

    arr = tifffile.imread(path)
    arr = np.squeeze(arr)
    if arr.ndim != 2:
        raise ValueError(f"expected a 2-D label mask in {path}, got shape {arr.shape}")
    return arr


def read_pyramid_lazy(path: str):
    """Lazily read the full-resolution level of a pyramidal OME-TIFF as (c, y, x).

    Dask-backed so peak memory is one chunk rather than the whole slide — the
    pyramid is the largest artifact the pipeline produces, and materializing it
    here would defeat the point of an out-of-core format.
    """
    import dask.array as da
    import tifffile

    store = tifffile.imread(path, aszarr=True, level=0)
    arr = da.from_zarr(store)
    if arr.ndim == 2:
        arr = arr[None, ...]
    if arr.ndim != 3:
        raise ValueError(f"expected 2-D or 3-D pyramid in {path}, got {arr.shape}")
    return arr


def channel_names(path: str, n: int) -> List[str]:
    """Channel names from OME-XML, falling back to positional names."""
    try:
        sys.path.insert(0, str(Path(__file__).parent / "utils"))
        from metadata import extract_channel_names_from_ome

        names = list(extract_channel_names_from_ome(path) or [])
        if len(names) == n:
            return names
        logger.warning(
            "OME-XML lists %d channels but the array has %d; using positional names",
            len(names),
            n,
        )
    except Exception as exc:  # noqa: BLE001 - metadata is advisory, never fatal here
        logger.warning("could not read channel names from %s: %s", path, exc)
    return [f"channel_{i}" for i in range(n)]


# ── table ──────────────────────────────────────────────────────────────────────
def build_table(
    quant_csv: str,
    patient_id: str,
    reg_residuals: Optional[pd.DataFrame],
    qc: Dict,
    extras: Dict,
    residual_stats: Dict,
):
    """Assemble the AnnData table and attach the SpatialData region metadata."""
    import anndata as ad
    from spatialdata.models import TableModel

    df = pd.read_csv(quant_csv)
    if INSTANCE_KEY not in df.columns:
        raise ValueError(
            f"{quant_csv} has no '{INSTANCE_KEY}' column — it is the instance key that "
            "ties every table row to a mask label; without it the object cannot be built."
        )
    df = df.drop_duplicates(subset=INSTANCE_KEY, keep="first").reset_index(drop=True)

    markers = identify_marker_columns(df)
    if not markers:
        raise ValueError(f"no marker intensity columns found in {quant_csv}")

    X = df[markers].to_numpy(dtype=np.float32)

    parsed = [parse_measurement_key(m) for m in markers]
    var = pd.DataFrame(
        {
            "marker": [p[0] for p in parsed],
            "compartment": [p[1] for p in parsed],
            "statistic": [p[2] for p in parsed],
        },
        index=pd.Index(markers, name="measurement"),
    )

    obs = pd.DataFrame(index=pd.RangeIndex(len(df)).astype(str))
    obs[INSTANCE_KEY] = df[INSTANCE_KEY].to_numpy(dtype=np.int64)
    obs["patient_id"] = patient_id
    for col in MORPHOLOGY_COLS:
        if col in df.columns and col not in (INSTANCE_KEY, "x", "y"):
            obs[f"qc_{col}" if col not in ("fov",) else col] = df[col].to_numpy()
    # region_key MUST be categorical and its categories must match `region` exactly.
    obs[REGION_KEY] = pd.Categorical(
        [REGION_LABELS] * len(df), categories=[REGION_LABELS]
    )

    adata = ad.AnnData(X=X, obs=obs, var=var)

    if {"x", "y"} <= set(df.columns):
        # Pixels, not micrometres: SpatialData's convention is intrinsic pixel
        # coordinates plus a transformation carrying the scale. Storing µm here
        # would double-apply pixel_size on any cross-modality alignment.
        adata.obsm["spatial"] = df[["x", "y"]].to_numpy(dtype=np.float64)

    # Per-slide z-scores, matching what EXPORT_GEOJSON writes for QuPath display.
    with np.errstate(invalid="ignore", divide="ignore"):
        mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
        adata.layers["zscore"] = np.where(
            sd > 0, (X - mu) / np.where(sd > 0, sd, 1), 0.0
        )

    if reg_residuals is not None and not reg_residuals.empty:
        aligned = reg_residuals.reindex(obs[INSTANCE_KEY].to_numpy())
        adata.obsm["qc_reg_residual_px"] = aligned.to_numpy(dtype=np.float64)
        adata.uns["qc_reg_residual_columns"] = [str(c) for c in aligned.columns]
        finite = np.isfinite(aligned.to_numpy(dtype=np.float64))
        adata.obs["qc_reg_matched"] = finite.any(axis=1)
        with np.errstate(invalid="ignore"):
            adata.obs["qc_reg_residual_max_px"] = (
                np.nanmax(
                    np.where(finite, aligned.to_numpy(dtype=np.float64), np.nan),
                    axis=1,
                    initial=np.nan,
                )
                if finite.any()
                else np.nan
            )

    adata.uns["qc"] = sanitize_for_uns(qc)
    adata.uns["qc_json"] = extras["qc_json"]
    adata.uns["qc_reg_residual_join"] = sanitize_for_uns(residual_stats)
    adata.uns["provenance"] = sanitize_for_uns(
        {"patient_id": patient_id, "versions": extras["versions"]}
    )

    return TableModel.parse(
        adata,
        region=REGION_LABELS,
        region_key=REGION_KEY,
        instance_key=INSTANCE_KEY,
    )


# ── assembly ───────────────────────────────────────────────────────────────────
def build_spatialdata(args) -> "object":
    """Assemble the SpatialData object from MIRAGE artifacts."""
    from spatialdata import SpatialData
    from spatialdata.models import Image2DModel, Labels2DModel, ShapesModel
    from spatialdata.transformations import Identity, Scale

    px = float(args.pixel_size)

    # Two coordinate systems: 'global' is intrinsic pixels (what every element is
    # stored in), 'um' scales it to micrometres. Every element carries both, so
    # they stay mutually aligned in either.
    def transforms():
        return {"global": Identity(), "um": Scale([px, px], axes=("y", "x"))}

    labels_elems, shapes_elems, images_elems = {}, {}, {}

    cell_mask = read_mask(args.cell_mask)
    labels_elems[REGION_LABELS] = Labels2DModel.parse(
        cell_mask, dims=("y", "x"), transformations=transforms()
    )
    logger.info("  labels/%s: %s", REGION_LABELS, cell_mask.shape)

    if args.nuclei_mask:
        nuc = read_mask(args.nuclei_mask)
        labels_elems["nuclei_mask"] = Labels2DModel.parse(
            nuc, dims=("y", "x"), transformations=transforms()
        )
        logger.info("  labels/nuclei_mask: %s", nuc.shape)

    quant = pd.read_csv(args.quant_csv).drop_duplicates(subset=INSTANCE_KEY)
    labels_arr = quant[INSTANCE_KEY].to_numpy(dtype=np.int64)

    cells_gdf = build_shapes(args.contours_json, labels_arr, "cells")
    if cells_gdf is not None:
        shapes_elems["cells"] = ShapesModel.parse(
            cells_gdf, transformations=transforms()
        )
    if args.nucleus_contours_json:
        nuc_gdf = build_shapes(args.nucleus_contours_json, labels_arr, "nuclei")
        if nuc_gdf is not None:
            shapes_elems["nuclei"] = ShapesModel.parse(
                nuc_gdf, transformations=transforms()
            )

    if args.include_image and args.pyramid:
        arr = read_pyramid_lazy(args.pyramid)
        names = channel_names(args.pyramid, arr.shape[0])
        images_elems["pyramid"] = Image2DModel.parse(
            arr,
            dims=("c", "y", "x"),
            c_coords=names,
            scale_factors=[2, 2, 2, 2],
            transformations=transforms(),
        )
        logger.info("  images/pyramid: %s (%d channels, lazy)", arr.shape, len(names))
    elif args.include_image:
        logger.warning("--include-image given but no --pyramid; skipping the image")

    centroids = (
        quant[["x", "y"]].to_numpy(dtype=float)
        if {"x", "y"} <= set(quant.columns)
        else np.empty((0, 2))
    )
    residuals, residual_stats = join_reg_residuals(
        args.reg_residuals or [],
        centroids,
        labels_arr,
        args.residual_join_max_px,
        patient_id=args.patient_id,
    )

    qc, extras = load_qc(
        args.qc_json or [], args.versions or [], patient_id=args.patient_id
    )
    table = build_table(
        args.quant_csv, args.patient_id, residuals, qc, extras, residual_stats
    )

    return SpatialData(
        images=images_elems,
        labels=labels_elems,
        shapes=shapes_elems,
        tables={"table": table},
    )


# ── attach mode ────────────────────────────────────────────────────────────────
def attach_phenotypes(zarr_path: str, csv_path: str, gate_tree: Optional[str]) -> None:
    """Add FlowPath gating results to an existing store.

    Joins on ``label``. FlowPath's own ``cell_id`` is a positional index into a
    QuPath detection collection whose order is not guaranteed, so a positional
    join would shift phenotype assignments and produce plausible-looking wrong
    biology rather than an error. Hence: join on ``label``, and refuse otherwise.
    """
    import spatialdata as sd

    sdata = sd.read_zarr(zarr_path)
    table = sdata.tables["table"]
    pheno = pd.read_csv(csv_path)

    if INSTANCE_KEY not in pheno.columns:
        raise ValueError(
            f"{csv_path} has no '{INSTANCE_KEY}' column. Refusing to align positionally: "
            "FlowPath's cell_id is a collection index, not a mask label, so a positional "
            "join silently mis-assigns phenotypes. Re-export with the label measurement."
        )

    pheno = pheno.drop_duplicates(subset=INSTANCE_KEY, keep="first").set_index(
        INSTANCE_KEY
    )
    order = pd.Index(table.obs[INSTANCE_KEY].to_numpy())
    missing = order.difference(pheno.index)
    if len(missing) == len(order):
        raise ValueError(
            f"no labels in {csv_path} match the table's instance key — wrong patient?"
        )
    if len(missing):
        logger.warning(
            "%d of %d cells have no phenotype row; they will be marked unclassified",
            len(missing),
            len(order),
        )

    aligned = pheno.reindex(order)
    if "phenotype" in aligned.columns:
        table.obs["phenotype"] = pd.Categorical(
            aligned["phenotype"].fillna("unclassified")
        )
    for src, dst in (
        ("Outlier", "fp_outlier"),
        ("Out_of_annotation", "fp_out_of_annotation"),
    ):
        if src in aligned.columns:
            table.obs[dst] = aligned[src].fillna(False).astype(bool).to_numpy()

    # `_sign` is three-state: "+", not-"+", or blank when no gate ever touched that
    # column. Keeping only gated columns preserves that — a boolean cast over all
    # markers would merge "negative" with "never gated".
    sign_cols = [c for c in aligned.columns if c.endswith("_sign")]
    gated = [
        c for c in sign_cols if aligned[c].notna().any() and (aligned[c] != "").any()
    ]
    if gated:
        pos = (aligned[gated] == "+").to_numpy(dtype=bool)
        table.obsm["positivity"] = pos
        table.uns["positivity_columns"] = [c[: -len("_sign")] for c in gated]
        logger.info("  positivity: %d gated columns", len(gated))

    if gate_tree:
        table.uns.setdefault("flowpath", {})["gate_tree"] = Path(gate_tree).read_text()

    sdata.delete_element_from_disk("table")
    sdata.write_element("table")
    logger.info("Attached phenotypes to %s", zarr_path)


# ── CLI ────────────────────────────────────────────────────────────────────────
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--attach-phenotypes", help="FlowPath phenotype CSV (attach mode)")
    ap.add_argument("--gate-tree", help="FlowPath gate-tree JSON (attach mode)")
    ap.add_argument("--zarr", help="existing .zarr to update (attach mode)")

    ap.add_argument("--quant-csv", help="merged quantification CSV")
    ap.add_argument("--contours-json", help="whole-cell contours JSON")
    ap.add_argument("--nucleus-contours-json", help="nucleus contours JSON (re-keyed)")
    ap.add_argument("--cell-mask", help="cell label mask TIFF")
    ap.add_argument("--nuclei-mask", help="nuclei label mask TIFF")
    ap.add_argument("--pyramid", help="merged pyramidal OME-TIFF")
    ap.add_argument("--qc-json", nargs="*", help="registration QC JSONs")
    ap.add_argument("--reg-residuals", nargs="*", help="per-cell residual CSVs")
    ap.add_argument("--versions", nargs="*", help="versions.yml files")
    ap.add_argument("--patient-id", default="unknown")
    ap.add_argument("--pixel-size", type=float, default=0.325)
    ap.add_argument(
        "--include-image",
        action="store_true",
        help="write the pyramid into the store (duplicates the largest artifact on disk)",
    )
    ap.add_argument(
        "--residual-join-max-px",
        type=float,
        default=15.0,
        help="max centroid distance for the QC->cell_mask spatial join",
    )
    ap.add_argument("-o", "--output", help="output .zarr path")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    configure_logging(level=logging.INFO)
    a = parse_args(argv)

    if a.attach_phenotypes:
        if not a.zarr:
            raise SystemExit("--attach-phenotypes requires --zarr")
        attach_phenotypes(a.zarr, a.attach_phenotypes, a.gate_tree)
        return 0

    for req in ("quant_csv", "contours_json", "cell_mask", "output"):
        if not getattr(a, req):
            raise SystemExit(f"--{req.replace('_', '-')} is required in pipeline mode")

    logger.info("Building SpatialData for %s", a.patient_id)
    sdata = build_spatialdata(a)
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    sdata.write(out, overwrite=True)
    logger.info("Wrote %s", out)
    logger.info("%s", sdata)
    return 0


if __name__ == "__main__":
    sys.exit(main())
