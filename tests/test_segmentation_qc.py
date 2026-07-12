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


def test_run_reports_before_after_and_delta():
    # after (registered) perfectly overlaps; before (unregistered) is disjoint.
    aligned = _mask((0, 2, 0, 2), (4, 6, 4, 6))
    ref_pre = _mask((0, 2, 0, 2))
    mov_pre = _mask((5, 7, 5, 7))  # misaligned before registration
    seg = {"ref_reg.tif": aligned, "mov_reg.tif": aligned.copy(),
           "ref_pre.tif": ref_pre, "mov_pre.tif": mov_pre}
    out = segmentation_qc.run(
        "ref_reg.tif", "mov_reg.tif", "ref_pre.tif", "mov_pre.tif",
        segmenter=lambda p: seg[p],
    )
    assert out["after"]["dice"] == 1.0 and out["after"]["instance_f1"] == 1.0
    assert out["before"]["dice"] == 0.0
    # delta = after - before shows the registration improvement
    assert out["delta"]["dice"] == 1.0
    assert out["delta"]["instance_f1"] == 1.0


def test_run_delta_is_after_minus_before():
    # half-overlap before, full after
    ref_pre = _mask((0, 2, 0, 4), shape=(8, 8))
    mov_pre = _mask((0, 2, 2, 6), shape=(8, 8))  # partial overlap
    aligned = _mask((0, 2, 0, 4), shape=(8, 8))
    seg = {"ref_reg.tif": aligned, "mov_reg.tif": aligned.copy(),
           "ref_pre.tif": ref_pre, "mov_pre.tif": mov_pre}
    out = segmentation_qc.run(
        "ref_reg.tif", "mov_reg.tif", "ref_pre.tif", "mov_pre.tif",
        segmenter=lambda p: seg[p],
    )
    for k in ("dice", "iou", "instance_f1"):
        assert out["delta"][k] == out["after"][k] - out["before"][k]


def test_build_record_carries_identity_and_metrics():
    result = {"before": {"dice": 0.3}, "after": {"dice": 0.9}, "delta": {"dice": 0.6}}
    rec = segmentation_qc.build_record(result, patient_id="P1",
                                       registered="P1_CD3_registered.ome.tiff",
                                       reference="P1_DAPI_registered.ome.tiff")
    assert rec["patient_id"] == "P1"
    assert rec["moving"] == "P1_CD3_registered.ome.tiff"
    assert rec["reference"] == "P1_DAPI_registered.ome.tiff"
    assert rec["after"]["dice"] == 0.9 and rec["delta"]["dice"] == 0.6


def test_write_report_roundtrips(tmp_path):
    out = tmp_path / "P1_CD3_seg_qc.json"
    seg = {"ref_reg.tif": _mask((0, 2, 0, 2)), "mov_reg.tif": _mask((0, 2, 0, 2)),
           "ref_pre.tif": _mask((0, 2, 0, 2)), "mov_pre.tif": _mask((0, 2, 0, 2))}
    segmentation_qc.write_report(
        "ref_reg.tif", "mov_reg.tif", "ref_pre.tif", "mov_pre.tif", str(out),
        patient_id="P1", segmenter=lambda p: seg[p],
    )
    data = json.loads(out.read_text())
    assert data["patient_id"] == "P1"
    assert data["after"]["dice"] == 1.0
    assert data["delta"]["dice"] == 0.0  # before already perfect here
