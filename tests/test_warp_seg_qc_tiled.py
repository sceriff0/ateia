"""Test the reg_qc=2 tiled dispatch: warp_seg_qc.py --method tiled, JVM-free.

Proves the reg_qc=2 seg-QC works for the STARE method by driving the *real* CLI ``main()`` with a
STARE manifest (M0 + mesh) instead of a VALIS pickle: no JVM, stages native/rigid/refined, and the
same scorer output shape the VALIS path produces. This closes the loop the Phase 1 unit seam
opened — now through the actual command the Nextflow module runs.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
pytest.importorskip("skimage")
pytest.importorskip("scipy")

import warp_seg_qc as wsq  # noqa: E402


def _square(x0, y0, s=10):
    ring = [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]]
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _grid_fc(n=6, pitch=40, dx=0.0):
    return {
        "type": "FeatureCollection",
        "features": [_square(i * pitch + dx, 0.0) for i in range(n)],
    }


def _write(p, obj):
    p.write_text(json.dumps(obj))
    return str(p)


def test_reg_qc2_tiled_cli_scores_through_a_manifest_without_a_jvm(tmp_path):
    # manifest: reference is identity; moving rigid-aligns to within 4px, a uniform mesh finishes it
    manifest = {
        "ref_slide": "ref",
        "slides": {
            "ref": {"M0": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "mesh": None},
            "mov": {
                "M0": [[1, 0, -96], [0, 1, 0], [0, 0, 1]],
                "mesh": {
                    "grid_x": [0.0, 1000.0],
                    "grid_y": [0.0, 1000.0],
                    "displacements": [
                        [[-4.0, 0.0], [-4.0, 0.0]],
                        [[-4.0, 0.0], [-4.0, 0.0]],
                    ],
                },
            },
        },
    }
    man_f = _write(tmp_path / "manifest.json", manifest)
    ref_gj = _write(tmp_path / "ref.geojson", _grid_fc())
    mov_gj = _write(tmp_path / "mov.geojson", _grid_fc(dx=100.0))
    out_f = tmp_path / "seg_qc.json"

    # returns None (no JVM path taken); would raise if it tried to import/boot VALIS
    wsq.main(
        [
            "--method",
            "tiled",
            "--pickle",
            man_f,
            "--ref-slide",
            "ref",
            "--moving-slide",
            "mov",
            "--ref-geojson",
            ref_gj,
            "--moving-geojson",
            mov_gj,
            "--output",
            str(out_f),
            "--patient-id",
            "P001",
            "--supersample",
            "4",
        ]
    )

    rec = json.loads(out_f.read_text())
    assert rec["stage_order"] == ["native", "rigid", "refined"]
    assert rec["stages_separable"] is True
    assert "micro_reg" not in rec  # a tiled run has no micro stage to report
    s = rec["stages"]
    assert s["rigid"]["displacement_px_p50"] == pytest.approx(4.0)
    assert s["refined"]["displacement_px_p50"] == pytest.approx(0.0, abs=1e-9)
    assert s["rigid"]["iou_mean"] < s["refined"]["iou_mean"]
    assert rec["matching"]["n_pairs"] == 6
