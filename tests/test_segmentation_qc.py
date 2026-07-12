"""Tests for bin/segmentation_qc.py orchestration (in-pipeline reg_qc=2 QC).

StarDist/TensorFlow are lazy-imported inside the segmentation call, so this module
imports and its logic is testable by injecting a fake segmenter (path -> label mask).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

import segmentation_qc


def _mask(*boxes, shape=(8, 8)):
    m = np.zeros(shape, dtype=np.uint32)
    for i, (r0, r1, c0, c1) in enumerate(boxes, start=1):
        m[r0:r1, c0:c1] = i
    return m


def test_pick_dapi_index_finds_dapi_else_falls_back_to_zero():
    assert segmentation_qc.pick_dapi_index(["CD3", "DAPI", "SMA"]) == 1
    assert segmentation_qc.pick_dapi_index(["dapi_nuclei", "CD8"]) == 0  # case-insensitive
    assert segmentation_qc.pick_dapi_index(["CD3", "CD8"]) == 0          # fallback


def test_find_dapi_index_reads_real_ome_channel_names(tmp_path):
    # Exercises the real extract_channel_names_from_ome import path (regression:
    # the function lives in utils.metadata, not utils.qc).
    pytest.importorskip("tifffile")
    from pathlib import Path
    ome = Path(__file__).resolve().parent / "testdata" / "P001_ref.ome.tiff"
    if not ome.exists():
        pytest.skip("P001_ref.ome.tiff fixture not generated")
    idx = segmentation_qc.find_dapi_index(str(ome))
    assert idx == 0  # DAPI is the first channel in the fixture


def test_run_scores_two_masks_via_injected_segmenter():
    ref = _mask((0, 2, 0, 2), (4, 6, 4, 6))
    mov = ref.copy()
    seg = {"ref.tif": ref, "reg.tif": mov}
    out = segmentation_qc.run("reg.tif", "ref.tif", segmenter=lambda p: seg[p])
    assert out["dice"] == 1.0
    assert out["instance_f1"] == 1.0
    assert out["n_ref"] == 2 and out["n_moving"] == 2


def test_run_detects_misalignment():
    ref = _mask((0, 2, 0, 2))
    mov = _mask((5, 7, 5, 7))  # disjoint
    seg = {"ref.tif": ref, "reg.tif": mov}
    out = segmentation_qc.run("reg.tif", "ref.tif", segmenter=lambda p: seg[p])
    assert out["dice"] == 0.0
    assert out["instance_f1"] == 0.0


def test_build_record_carries_identity_fields():
    metrics = {"dice": 0.9, "iou": 0.8, "instance_f1": 0.7}
    rec = segmentation_qc.build_record(metrics, patient_id="P1",
                                       registered="P1_CD3_registered.ome.tiff",
                                       reference="P1_DAPI_registered.ome.tiff")
    assert rec["patient_id"] == "P1"
    assert rec["moving"] == "P1_CD3_registered.ome.tiff"
    assert rec["reference"] == "P1_DAPI_registered.ome.tiff"
    assert rec["dice"] == 0.9 and rec["instance_f1"] == 0.7


def test_write_report_roundtrips(tmp_path):
    out = tmp_path / "P1_CD3_seg_qc.json"
    seg = {"ref.tif": _mask((0, 2, 0, 2)), "reg.tif": _mask((0, 2, 0, 2))}
    segmentation_qc.write_report(
        "reg.tif", "ref.tif", str(out),
        patient_id="P1", segmenter=lambda p: seg[p],
    )
    data = json.loads(out.read_text())
    assert data["patient_id"] == "P1"
    assert data["dice"] == 1.0
