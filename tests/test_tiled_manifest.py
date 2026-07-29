"""Tests for bin/utils/tiled_manifest.py — assembling the STARE transform manifest.

Per-tile residuals (dx, dy, tre) are laid onto the control grid, TRE-gated so only tiles whose
rigid misalignment exceeds the threshold contribute a displacement (well-aligned tiles stay at
their rigid position). The manifest round-trips through JSON and is consumed unchanged by
``tiled_stage_warp.make_warper`` — the same object the reg_qc=2 scorer and the image warp read.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)

from tile_grid import tile_grid
from tiled_manifest import assemble_control_grid, build_manifest, slide_entry
from tiled_stage_warp import STAGE_REFINED, STAGE_RIGID, make_warper


def test_assemble_lays_residuals_on_the_grid_and_gates_by_tre():
    tiles = tile_grid(100, 60, 40, halo=8)  # 3 cols x 2 rows
    # residual per tile: give tile (ix=0,iy=0) a big misalignment, others tiny
    residuals = []
    for t in tiles:
        if (t.ix, t.iy) == (0, 0):
            residuals.append((5.0, -3.0, np.hypot(5.0, 3.0)))  # tre ~5.83, above gate
        else:
            residuals.append(
                (0.02, 0.01, np.hypot(0.02, 0.01))
            )  # tre ~0.02, below gate

    gx, gy, disp = assemble_control_grid(tiles, residuals, gate_tre=1.0)
    disp = np.asarray(disp)

    assert len(gx) == 3 and len(gy) == 2
    assert disp.shape == (2, 3, 2)
    # the high-TRE tile keeps its correction; every gated-out tile is zero
    np.testing.assert_allclose(disp[0, 0], [5.0, -3.0])
    others = [disp[iy, ix] for iy in range(2) for ix in range(3) if (ix, iy) != (0, 0)]
    np.testing.assert_allclose(others, 0.0)


def test_all_below_gate_yields_a_rigid_only_entry():
    tiles = tile_grid(80, 40, 40, halo=4)
    residuals = [(0.01, 0.0, 0.01) for _ in tiles]
    gx, gy, disp = assemble_control_grid(tiles, residuals, gate_tre=1.0)
    entry = slide_entry(np.eye(3), gx, gy, disp)
    assert (
        entry["mesh"] is None
    )  # nothing to refine -> no mesh, warper falls back to rigid


def test_manifest_round_trips_through_json_into_a_working_warper():
    tiles = tile_grid(100, 100, 50, halo=8)  # 2x2
    # one tile carries a real correction; assemble + build manifest
    residuals = [
        (4.0, 0.0, 4.0) if (t.ix, t.iy) == (1, 1) else (0.0, 0.0, 0.0) for t in tiles
    ]
    gx, gy, disp = assemble_control_grid(tiles, residuals, gate_tre=1.0)

    manifest = build_manifest(
        "ref",
        {
            "ref": slide_entry(np.eye(3)),
            # identity M0 so the rigid position equals the native point, landing exactly on the
            # (1,1) control node — makes the mesh contribution assertable to the node value.
            "mov": slide_entry(np.eye(3), gx, gy, disp),
        },
    )
    # must survive serialization intact (all json-native types)
    manifest = json.loads(json.dumps(manifest))

    warp = make_warper(manifest)
    # a point at the (1,1) tile centre (a control node): rigid is identity, mesh adds +4 in x
    centre = np.array([[gx[1], gy[1]]])
    rigid = warp("mov", centre, STAGE_RIGID)
    refined = warp("mov", centre, STAGE_REFINED)
    np.testing.assert_allclose(rigid, centre)
    np.testing.assert_allclose(refined, centre + np.array([4.0, 0.0]))
