#!/usr/bin/env python3
"""Segment one slide's DAPI on its NATIVE image and write a cell GeoJSON (reg_qc=2).

Thin orchestration for the GeoJSON/warp registration QC: extract + normalize DAPI
(auto-detected by OME channel name), StarDist-segment nuclei -> whole-cell mask,
then trace polygons (mask_to_geojson). Runs in the StarDist container. Heavy deps
(StarDist/TensorFlow) are imported lazily so the DAPI-index logic stays unit-testable.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def find_nuclear_index(image_path, nuclear_markers=None) -> int:
    """Return the nuclear-channel index for ``image_path``, resolved by marker name
    from its OME channel names (never the filename).

    Falls back to index 0 when no listed marker matches: images reaching this QC
    path are CONVERT_IMAGE output, where the nuclear channel is already normalized
    to channel 0.
    """
    from utils.metadata import extract_channel_names_from_ome, pick_nuclear_index

    names = extract_channel_names_from_ome(image_path)
    # pick_nuclear_index already falls back to DEFAULT_NUCLEAR_MARKERS when the list is
    # empty; naming the constant a second time here only created another place for the
    # fallback to drift. SEG_QC_GEOJSON now passes params.nuclear_markers explicitly.
    idx = pick_nuclear_index(names, nuclear_markers)
    return idx if idx is not None else 0


def segment_to_geojson(
    image_path,
    out_geojson,
    model_dir,
    model_name,
    use_gpu=False,
    dapi_channel=None,
    nuclear_markers=None,
    pmin=1.0,
    pmax=99.8,
    n_tiles=(1, 1),
    expand_distance=10,
    prob_thresh=None,
    simplify_tolerance=1.0,
) -> int:
    """Segment DAPI nuclei to whole-cell polygons and write them as a GeoJSON.

    Returns the number of cell features written.
    """
    import mask_to_geojson
    import segment

    idx = (
        dapi_channel
        if dapi_channel is not None
        else find_nuclear_index(image_path, nuclear_markers)
    )
    dapi, _ = segment.extract_dapi_channel(str(image_path), idx)
    normalized = segment.normalize_dapi(dapi, pmin=pmin, pmax=pmax)
    model = segment.load_stardist_model(model_dir, model_name, use_gpu=use_gpu)
    _, cell_mask = segment.segment_nuclei(
        normalized,
        model,
        n_tiles=tuple(n_tiles),
        expand_distance=expand_distance,
        prob_thresh=prob_thresh,
    )
    fc = mask_to_geojson.mask_to_feature_collection(
        cell_mask, simplify_tolerance=simplify_tolerance
    )
    import json

    with open(out_geojson, "w") as f:
        json.dump(fc, f)
    return len(fc["features"])


def parse_args(argv=None):
    """Parse command-line arguments."""
    ap = argparse.ArgumentParser(description="Segment a slide's DAPI -> cell GeoJSON.")
    ap.add_argument("--image", required=True, help="native (pre-registration) OME-TIFF")
    ap.add_argument("--output", required=True, help="output cell GeoJSON")
    ap.add_argument(
        "--dapi-channel",
        type=int,
        default=None,
        help="DAPI channel index; auto-detected from OME channel names if omitted",
    )
    ap.add_argument(
        "--nuclear-markers",
        nargs="+",
        default=None,
        help="Marker names identifying the nuclear channel, used when --dapi-channel is "
        "omitted. SEG_QC_GEOJSON always passes params.nuclear_markers; the default is "
        "only for standalone use.",
    )
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--model-name", required=True)
    ap.add_argument("--use-gpu", action="store_true")
    ap.add_argument("--pmin", type=float, default=1.0)
    ap.add_argument("--pmax", type=float, default=99.8)
    ap.add_argument("--n-tiles", type=int, nargs=2, default=[1, 1], metavar=("Y", "X"))
    ap.add_argument("--expand-distance", type=int, default=10)
    ap.add_argument("--prob-thresh", type=float, default=None)
    # Matches nextflow.config's simplify_tolerance (1.0), which is also
    # mask_to_geojson.py's own standalone default: the pipeline always passes
    # --tolerance explicitly (conf/modules.config's SEG_QC_GEOJSON ext.args), so this
    # default only matters for standalone invocation, and should behave the same as
    # the pipeline by default.
    ap.add_argument("--tolerance", type=float, default=1.0)
    return ap.parse_args(argv)


def main(argv=None):
    """CLI entry point: segment a slide's DAPI channel and write a cell GeoJSON."""
    a = parse_args(argv)
    n = segment_to_geojson(
        a.image,
        a.output,
        a.model_dir,
        a.model_name,
        use_gpu=a.use_gpu,
        dapi_channel=a.dapi_channel,
        nuclear_markers=a.nuclear_markers,
        pmin=a.pmin,
        pmax=a.pmax,
        n_tiles=tuple(a.n_tiles),
        expand_distance=a.expand_distance,
        prob_thresh=a.prob_thresh,
        simplify_tolerance=a.tolerance,
    )
    print(f"Wrote {n} cells to {a.output}")


if __name__ == "__main__":
    main()
