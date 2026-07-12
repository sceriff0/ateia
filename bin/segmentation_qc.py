#!/usr/bin/env python
"""In-pipeline segmentation registration QC (reg_qc=2).

Segments the DAPI channel of a registered moving slide and its patient's registered
reference slide independently (StarDist, the pipeline default), then scores how well the
two nuclei masks overlap (Dice / IoU / instance-F1). Because both inputs are already
co-registered on the aligned grid, no coordinate warping is needed — masks are compared
directly (see utils/seg_overlap.py).

Reuses bin/segment.py for segmentation and bin/utils/qc.py for DAPI channel lookup. Heavy
deps (StarDist/TensorFlow) are imported lazily so the orchestration is unit-testable.

    segmentation_qc.py --registered R.ome.tiff --reference REF.ome.tiff \\
        --model-name 2D_versatile_fluo [--model-dir DIR] --output P1_CD3_seg_qc.json \\
        [--patient-id P1] [--dapi-channel N] [--use-gpu]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make sibling bin/ modules importable when run as a Nextflow script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pick_dapi_index(channel_names) -> int:
    """Index of the DAPI channel by name (case-insensitive), or 0 if none found."""
    for i, ch in enumerate(channel_names or []):
        if "DAPI" in str(ch).upper():
            return i
    return 0


def find_dapi_index(image_path) -> int:
    from utils.qc import extract_channel_names_from_ome  # light (tifffile + OME-XML)
    return pick_dapi_index(extract_channel_names_from_ome(image_path))


def segment_dapi(image_path, model, dapi_channel=None, pmin=1.0, pmax=99.8,
                 n_tiles=(1, 1), expand_distance=10, prob_thresh=None):
    """Extract + normalize the DAPI channel and return the StarDist whole-cell mask."""
    import segment  # lazy: pulls StarDist/TensorFlow only at real runtime
    idx = dapi_channel if dapi_channel is not None else find_dapi_index(image_path)
    dapi, _ = segment.extract_dapi_channel(str(image_path), idx)
    normalized = segment.normalize_dapi(dapi, pmin=pmin, pmax=pmax)
    _, cell_mask = segment.segment_nuclei(
        normalized, model, n_tiles=tuple(n_tiles),
        expand_distance=expand_distance, prob_thresh=prob_thresh,
    )
    return cell_mask


def _default_segmenter(model_dir, model_name, use_gpu, dapi_channel,
                       pmin, pmax, n_tiles, expand_distance, prob_thresh):
    import segment  # lazy
    model = segment.load_stardist_model(model_dir, model_name, use_gpu=use_gpu)
    return lambda path: segment_dapi(
        path, model, dapi_channel=dapi_channel, pmin=pmin, pmax=pmax,
        n_tiles=n_tiles, expand_distance=expand_distance, prob_thresh=prob_thresh,
    )


def run(registered, reference, segmenter=None, iou_thresh=0.5, **seg_kwargs) -> dict:
    """Segment reference + registered slides and score mask overlap.

    ``segmenter`` (path -> label mask) is injectable for tests; when omitted a StarDist
    segmenter is built from ``seg_kwargs`` (model_dir/model_name/use_gpu/...).
    """
    from utils.seg_overlap import evaluate_masks
    if segmenter is None:
        segmenter = _default_segmenter(
            seg_kwargs.get("model_dir"), seg_kwargs.get("model_name"),
            seg_kwargs.get("use_gpu", False), seg_kwargs.get("dapi_channel"),
            seg_kwargs.get("pmin", 1.0), seg_kwargs.get("pmax", 99.8),
            seg_kwargs.get("n_tiles", (1, 1)), seg_kwargs.get("expand_distance", 10),
            seg_kwargs.get("prob_thresh"),
        )
    ref_mask = segmenter(reference)
    mov_mask = segmenter(registered)
    return evaluate_masks(ref_mask, mov_mask, iou_thresh=iou_thresh)


def build_record(metrics, patient_id, registered, reference) -> dict:
    return {
        "patient_id": patient_id,
        "moving": os.path.basename(str(registered)),
        "reference": os.path.basename(str(reference)),
        **metrics,
    }


def write_report(registered, reference, output, patient_id=None,
                 segmenter=None, iou_thresh=0.5, **seg_kwargs) -> dict:
    metrics = run(registered, reference, segmenter=segmenter,
                  iou_thresh=iou_thresh, **seg_kwargs)
    record = build_record(metrics, patient_id, registered, reference)
    with open(output, "w") as f:
        json.dump(record, f, indent=2)
    return record


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Segmentation-overlap registration QC.")
    ap.add_argument("--registered", required=True, help="registered moving slide (OME-TIFF)")
    ap.add_argument("--reference", required=True, help="registered reference slide (OME-TIFF)")
    ap.add_argument("--output", required=True, help="output JSON path")
    ap.add_argument("--patient-id", default=None)
    ap.add_argument("--dapi-channel", type=int, default=None,
                    help="DAPI channel index; auto-detected from OME channel names if omitted")
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--model-name", required=True)
    ap.add_argument("--use-gpu", action="store_true")
    ap.add_argument("--pmin", type=float, default=1.0)
    ap.add_argument("--pmax", type=float, default=99.8)
    ap.add_argument("--n-tiles", type=int, nargs=2, default=[1, 1], metavar=("Y", "X"))
    ap.add_argument("--expand-distance", type=int, default=10)
    ap.add_argument("--prob-thresh", type=float, default=None)
    ap.add_argument("--iou-thresh", type=float, default=0.5)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    record = write_report(
        a.registered, a.reference, a.output, patient_id=a.patient_id,
        iou_thresh=a.iou_thresh, model_dir=a.model_dir, model_name=a.model_name,
        use_gpu=a.use_gpu, dapi_channel=a.dapi_channel, pmin=a.pmin, pmax=a.pmax,
        n_tiles=tuple(a.n_tiles), expand_distance=a.expand_distance,
        prob_thresh=a.prob_thresh,
    )
    print(f"Wrote {a.output}: dice={record['dice']:.4f} "
          f"iou={record['iou']:.4f} instance_f1={record['instance_f1']:.4f}")


if __name__ == "__main__":
    main()
