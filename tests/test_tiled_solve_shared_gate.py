"""``tiled_solve.py`` must gate through the SHARED control-grid assembly, not a local copy.

The TRE gate — "a tile whose rigid-stage TRE is below the threshold contributes a zero
displacement and stays rigid" — is the rule that decides which parts of a slide get refined.
It has one owner, :func:`tiled_manifest.assemble_control_grid`, which
``tiled_pipeline.register_slide`` calls. The fan-out reduction used to re-implement the same
~10 lines locally (``_grid_from_controls``), so an operator fix or an off-by-one applied to one
copy silently left the other behind — and the two copies are the tested path and the shipped
path respectively, which is the worst possible way round.

Two tests, deliberately different in kind:

  * a **delegation** test — monkeypatching the shared function changes what the manifest
    contains, which is only true if the shared function is actually the one being called;
  * an **equivalence** test at the gate boundary (``tre == gate_tre`` must refine, ``>=``
    not ``>``), which pins the rule itself rather than the call.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
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

import tiled_solve  # noqa: E402
from tile_grid import Tile  # noqa: E402
from tiled_manifest import assemble_control_grid, slide_entry  # noqa: E402

GATE = 1.0
MOVING = "CYCLE2"

# (ix, iy, cx, cy, dx, dy, tre) — one below the gate, one exactly ON it, two above.
CONTROLS = [
    (0, 0, 32.0, 32.0, 0.7, -0.4, 0.25),  # below  -> gated out, stays rigid
    (1, 0, 96.0, 32.0, 1.5, 2.5, GATE),  # exactly on the gate -> MUST refine (>=)
    (0, 1, 32.0, 96.0, -3.0, 1.0, 4.0),  # above  -> refines
    (1, 1, 96.0, 96.0, 2.0, -2.0, 9.0),  # above  -> refines
]


def _write_inputs(tmp_path, controls=CONTROLS):
    m0 = [[1.0, 0.0, 5.0], [0.0, 1.0, -3.0], [0.0, 0.0, 1.0]]
    m0_f = tmp_path / "m0.json"
    m0_f.write_text(
        json.dumps(
            {
                "M0": m0,
                "ref_h": 128,
                "ref_w": 128,
                "ref_name": "CYCLE1",
                "coarse_tre": 1.25,
                "n_inliers": 42,
                "coarse_factor": 4,
            }
        )
    )
    ctrl_dir = tmp_path / "ctrl"
    ctrl_dir.mkdir()
    for ix, iy, cx, cy, dx, dy, tre in controls:
        (ctrl_dir / f"{ix}_{iy}_ctrl.json").write_text(
            json.dumps(
                {"ix": ix, "iy": iy, "cx": cx, "cy": cy, "dx": dx, "dy": dy, "tre": tre}
            )
        )
    return m0_f, str(ctrl_dir / "*_ctrl.json"), np.asarray(m0, dtype=float)


def _run_solve(tmp_path, m0_f, glob_pat, gate=GATE):
    out_manifest = tmp_path / "manifest.json"
    rc = tiled_solve.main(
        [
            "--m0",
            str(m0_f),
            "--controls",
            glob_pat,
            "--gate-tre",
            str(gate),
            "--moving-name",
            MOVING,
            "--out-manifest",
            str(out_manifest),
            "--out-tre",
            str(tmp_path / "tre.json"),
        ]
    )
    assert rc == 0
    return json.loads(out_manifest.read_text())


def test_solve_delegates_the_gate_to_the_shared_assemble_control_grid(
    tmp_path, monkeypatch
):
    """Patching the shared function must change the manifest — proof it is the one called.

    ``monkeypatch.setattr`` fails outright if ``tiled_solve`` has no such attribute, so this
    also fails against the old module that kept its own ``_grid_from_controls`` and never
    imported the shared name.
    """
    m0_f, glob_pat, _m0 = _write_inputs(tmp_path)

    sentinel_disp = [[[11.0, 12.0], [13.0, 14.0]], [[15.0, 16.0], [17.0, 18.0]]]

    def fake_assemble(tiles, residuals, gate_tre):
        assert gate_tre == GATE
        return [111.0, 222.0], [333.0, 444.0], sentinel_disp

    monkeypatch.setattr(tiled_solve, "assemble_control_grid", fake_assemble)

    manifest = _run_solve(tmp_path, m0_f, glob_pat)
    mesh = manifest["slides"][MOVING]["mesh"]
    assert mesh is not None
    assert mesh["grid_x"] == [111.0, 222.0]
    assert mesh["grid_y"] == [333.0, 444.0]
    assert mesh["displacements"] == sentinel_disp


def test_solve_gates_identically_to_the_shared_rule_including_at_the_boundary(tmp_path):
    """The manifest equals what ``assemble_control_grid`` + ``slide_entry`` produce, exactly."""
    m0_f, glob_pat, m0 = _write_inputs(tmp_path)
    manifest = _run_solve(tmp_path, m0_f, glob_pat)

    tiles = [
        Tile(ix=ix, iy=iy, cx=cx, cy=cy, core=(0, 0, 1, 1), read=(0, 0, 1, 1))
        for ix, iy, cx, cy, _dx, _dy, _tre in CONTROLS
    ]
    residuals = [(dx, dy, tre) for _ix, _iy, _cx, _cy, dx, dy, tre in CONTROLS]
    grid_x, grid_y, disp = assemble_control_grid(tiles, residuals, GATE)
    expected = slide_entry(m0, grid_x, grid_y, disp)

    got = dict(manifest["slides"][MOVING])
    got.pop("out_shape")  # solve additionally carries the reference frame
    assert got == expected

    # ...and spell the boundary out, so the ">= vs >" rule is readable in the failure message
    d = got["mesh"]["displacements"]
    assert d[0][0] == [0.0, 0.0], "a below-gate tile must stay rigid"
    assert d[0][1] == [1.5, 2.5], "tre == gate_tre must REFINE (the rule is >=, not >)"
    assert d[1][0] == [-3.0, 1.0]
    assert d[1][1] == [2.0, -2.0]


def test_an_all_below_gate_slide_collapses_to_a_rigid_only_entry(tmp_path):
    """Every tile gated out -> no mesh at all, same as the shared rule's documented behaviour."""
    quiet = [(ix, iy, cx, cy, dx, dy, 0.1) for ix, iy, cx, cy, dx, dy, _t in CONTROLS]
    m0_f, glob_pat, _m0 = _write_inputs(tmp_path, controls=quiet)
    manifest = _run_solve(tmp_path, m0_f, glob_pat)
    assert manifest["slides"][MOVING]["mesh"] is None


def test_solve_no_longer_carries_its_own_copy_of_the_gate(tmp_path):
    """The duplicated implementation is gone, not merely bypassed."""
    assert not hasattr(tiled_solve, "_grid_from_controls"), (
        "tiled_solve still defines a private control-grid assembly; the gating rule has "
        "one owner, tiled_manifest.assemble_control_grid"
    )
