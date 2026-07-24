#!/usr/bin/env python
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


def pick_dapi_index(channel_names) -> int:
    for i, ch in enumerate(channel_names or []):
        if "DAPI" in str(ch).upper():
            return i
    return 0


def find_dapi_index(image_path) -> int:
    from utils.metadata import extract_channel_names_from_ome

    return pick_dapi_index(extract_channel_names_from_ome(image_path))


def segment_to_geojson(
    image_path,
    out_geojson,
    model_dir,
    model_name,
    use_gpu=False,
    dapi_channel=None,
    pmin=1.0,
    pmax=99.8,
    n_tiles=(1, 1),
    expand_distance=10,
    prob_thresh=None,
    simplify_tolerance=0.5,
) -> int:
    import mask_to_geojson
    import segment

    idx = dapi_channel if dapi_channel is not None else find_dapi_index(image_path)
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
    ap = argparse.ArgumentParser(description="Segment a slide's DAPI -> cell GeoJSON.")
    ap.add_argument("--image", required=True, help="native (pre-registration) OME-TIFF")
    ap.add_argument("--output", required=True, help="output cell GeoJSON")
    ap.add_argument(
        "--dapi-channel",
        type=int,
        default=None,
        help="DAPI channel index; auto-detected from OME channel names if omitted",
    )
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--model-name", required=True)
    ap.add_argument("--use-gpu", action="store_true")
    ap.add_argument("--pmin", type=float, default=1.0)
    ap.add_argument("--pmax", type=float, default=99.8)
    ap.add_argument("--n-tiles", type=int, nargs=2, default=[1, 1], metavar=("Y", "X"))
    ap.add_argument("--expand-distance", type=int, default=10)
    ap.add_argument("--prob-thresh", type=float, default=None)
    ap.add_argument("--tolerance", type=float, default=0.5)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    n = segment_to_geojson(
        a.image,
        a.output,
        a.model_dir,
        a.model_name,
        use_gpu=a.use_gpu,
        dapi_channel=a.dapi_channel,
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
