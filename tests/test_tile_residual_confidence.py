"""Confidence and range gating for STARE mesh control points.

Phase correlation always returns a peak. On a background tile, or a tile that straddles the
section edge, that peak is noise — but ``residual_displacement`` used to discard scikit-image's
normalised correlation error, and ``_grid_from_controls`` gated purely on displacement
*magnitude*. Background and section-edge tiles are the majority of a whole-slide grid and
``MeshField`` spreads every control point bilinearly ±``reg_tiled_tile`` px into its neighbours,
so a spurious control point pollutes real tissue.

Measured on the synthetic tiles below (seed 1234, the same whiten + Hann + ``upsample=10``,
``normalization=None`` kernel ``residual_displacement`` uses):

    case                          dx       dy      |d|      error
    tissue, true 4 px shift     4.00     0.00     4.00   0.040956
    tissue, true 0 px shift     0.00     0.00     0.00   0.000000
    blank vs blank            -15.10     3.00    15.40   0.999469
    tissue vs blank            -5.30    -2.00     5.66   0.999961
    tissue vs zeros            -0.70    -0.70     0.99   nan

Note the section-edge case (tissue vs blank): |d| = 5.66 px is *inside* ``reg_tiled_halo`` (256)
and *above* ``reg_tiled_gate_tre`` (1.0). A magnitude bound cannot tell it from an ordinary small
residual. Only the error — 0.999961 against tissue's 0.040956 — exposes it. The magnitude bound is
kept as a cheap secondary net; the confidence gate does the real work.

The threshold asserted here (``MAX_ERROR``) has margin on both sides of the measured values, and
each test prints what it measured so the choice stays auditable.
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "bin"))
sys.path.insert(0, os.path.join(_ROOT, "bin", "utils"))
pytest.importorskip("skimage")
pytest.importorskip("scipy")

import tiled_solve  # noqa: E402
from tile_residual import residual_displacement  # noqa: E402

SEED = 1234
N = 256  # tile side (px)
PAD = 64  # margin the offset crops are taken from

# The gates under test, in the units the pipeline uses.
GATE_TRE = 1.0  # nextflow.config reg_tiled_gate_tre — refine only tiles above this
HALO = 256  # nextflow.config reg_tiled_halo — the physically-motivated |d| bound
MAX_ERROR = 0.99  # nextflow.config reg_tiled_max_error — confidence bound (see module docstring)


def _tissue_field(rng):
    """ONE textured field. Both crops of the tissue case must come from this single field.

    Generating it twice advances the RNG and yields two DIFFERENT fields, which would silently
    turn the "true 4 px shift" case into an unrelated-images case and invalidate the test.
    """
    size = N + 2 * PAD
    field = np.zeros((size, size), dtype=float)
    yy, xx = np.mgrid[0:size, 0:size]
    for _ in range(40):
        cy, cx = rng.integers(PAD // 2, N + PAD + PAD // 2, 2)
        field += 800.0 * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 6.0**2)))
    field += rng.normal(30.0, 3.0, field.shape)
    return field


def _tiles():
    """The four measured cases, plus the manufactured all-zeros moving tile.

    Returns a dict of ``name -> (ref_tile, mov_tile)``. One RNG, drawn in a fixed order, so every
    test in this module sees byte-identical tiles.
    """
    rng = np.random.default_rng(SEED)
    field = _tissue_field(rng)

    def crop(dy, dx):
        return field[PAD + dy : PAD + dy + N, PAD + dx : PAD + dx + N].copy()

    def blank():
        return rng.normal(30.0, 3.0, (N, N))

    return {
        "tissue_4px": (crop(0, 0), crop(0, 4)),
        "tissue_0px": (crop(0, 0), crop(0, 0)),
        "blank_vs_blank": (blank(), blank()),
        "tissue_vs_blank": (crop(0, 0), blank()),
        "tissue_vs_zeros": (crop(0, 0), np.zeros((N, N), dtype=float)),
    }


def _control(ix, iy, dx, dy, error, cx=None, cy=None):
    """A control point in exactly the shape ``tiled_reg_tile.py`` writes."""
    c = {
        "ix": ix,
        "iy": iy,
        "cx": float(ix * 2048 + 1024) if cx is None else float(cx),
        "cy": float(iy * 2048 + 1024) if cy is None else float(cy),
        "dx": float(dx),
        "dy": float(dy),
        "tre": float(math.hypot(dx, dy)),
    }
    if error is not None:
        c["error"] = float(error)
    return c


# ---------------------------------------------------------------------------
# 1. residual_displacement surfaces the correlation error
# ---------------------------------------------------------------------------


def test_residual_displacement_returns_the_correlation_error_as_a_fourth_value():
    """The error separates real tissue from background/section-edge garbage; it must be returned."""
    tiles = _tiles()
    measured = {}
    for name, (ref, mov) in tiles.items():
        dx, dy, tre, error = residual_displacement(ref, mov, upsample=10)
        measured[name] = (dx, dy, tre, error)

    table = "\n".join(
        f"    {n:16} dx={v[0]:8.2f} dy={v[1]:8.2f} |d|={v[2]:8.2f} error={v[3]:.6f}"
        for n, v in measured.items()
    )

    tissue_err = measured["tissue_4px"][3]
    aligned_err = measured["tissue_0px"][3]
    blank_err = measured["blank_vs_blank"][3]
    edge_err = measured["tissue_vs_blank"][3]

    # the real shift is still recovered correctly
    assert measured["tissue_4px"][0] == pytest.approx(4.0, abs=0.2), table
    assert measured["tissue_4px"][1] == pytest.approx(0.0, abs=0.2), table

    # ordering: both garbage cases score far worse than either real case
    assert tissue_err < blank_err, f"tissue must beat blank/blank:\n{table}"
    assert tissue_err < edge_err, f"tissue must beat the section edge:\n{table}"
    assert aligned_err < blank_err, table

    # and the chosen threshold has margin on BOTH sides of the measured values
    assert tissue_err < MAX_ERROR - 0.04, (
        f"tissue error {tissue_err:.6f} lacks margin below MAX_ERROR={MAX_ERROR}:\n{table}"
    )
    assert blank_err > MAX_ERROR + 0.005, (
        f"blank/blank error {blank_err:.6f} lacks margin above MAX_ERROR={MAX_ERROR}:\n{table}"
    )
    assert edge_err > MAX_ERROR + 0.005, (
        f"section-edge error {edge_err:.6f} lacks margin above MAX_ERROR={MAX_ERROR}:\n{table}"
    )


# ---------------------------------------------------------------------------
# 2. a confident, in-range control point is accepted
# ---------------------------------------------------------------------------


def test_grid_accepts_a_confident_control_point_between_the_gate_and_the_halo():
    controls = [_control(ix=0, iy=0, dx=4.0, dy=0.0, error=0.041)]
    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )
    assert disp[0][0] == [4.0, 0.0], (
        f"a tissue-confidence control point with gate_tre <= |d| < halo must be "
        f"accepted; got {disp[0][0]!r} (error=0.041, |d|=4.00, halo={HALO})"
    )


# ---------------------------------------------------------------------------
# 3. the section-edge case a magnitude bound cannot catch
# ---------------------------------------------------------------------------


def test_grid_rejects_the_section_edge_tile_that_the_magnitude_bound_misses():
    """|d| = 5.66 px is inside the halo and above the gate — only the error exposes it."""
    tiles = _tiles()
    ref, mov = tiles["tissue_vs_blank"]
    dx, dy, tre, error = residual_displacement(ref, mov, upsample=10)
    ctx = f"measured section-edge tile: dx={dx:.2f} dy={dy:.2f} |d|={tre:.2f} error={error:.6f}"

    # the premise: a magnitude bound would wave this straight through
    assert GATE_TRE <= tre < HALO, (
        f"premise broken — this case must be inside the halo and above the gate, "
        f"or it does not test what it claims. {ctx}"
    )

    controls = [_control(ix=0, iy=0, dx=dx, dy=dy, error=error)]
    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )
    assert disp[0][0] == [0.0, 0.0], (
        f"a low-confidence control point must leave [0.0, 0.0] in the grid; "
        f"got {disp[0][0]!r}. {ctx}"
    )


# ---------------------------------------------------------------------------
# 4. the magnitude bound is still the secondary net
# ---------------------------------------------------------------------------


def test_grid_rejects_a_displacement_at_or_beyond_the_halo_whatever_its_error():
    """A match further than the read halo was never inside the read window — an artefact."""
    controls = [
        _control(ix=0, iy=0, dx=float(HALO), dy=0.0, error=0.01),  # perfect confidence
        _control(ix=1, iy=0, dx=float(HALO) + 40.0, dy=0.0, error=0.01),
    ]
    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )
    assert disp[0][0] == [0.0, 0.0], (
        f"|d| == halo ({HALO}) must be rejected even at error=0.01; got {disp[0][0]!r}"
    )
    assert disp[0][1] == [0.0, 0.0], (
        f"|d| > halo must be rejected even at error=0.01; got {disp[0][1]!r}"
    )


# ---------------------------------------------------------------------------
# 5. median filtering over the accepted control points
# ---------------------------------------------------------------------------


def test_median_filter_pulls_an_outlier_toward_its_agreeing_neighbours():
    """One interior control point disagrees with 8 agreeing neighbours; the median pulls it back.

    Every point here passes both gates (error 0.05, |d| well inside the halo), so nothing is
    rejected — this isolates the smoothing step from the rejection step.
    """
    controls = []
    for iy in range(3):
        for ix in range(3):
            if (ix, iy) == (1, 1):
                controls.append(_control(ix, iy, dx=50.0, dy=50.0, error=0.05))
            else:
                controls.append(_control(ix, iy, dx=2.0, dy=2.0, error=0.05))

    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )

    assert disp[1][1] == [2.0, 2.0], (
        f"the interior outlier [50.0, 50.0] must be pulled to its neighbourhood median "
        f"[2.0, 2.0]; got {disp[1][1]!r} (an unfiltered grid returns [50.0, 50.0])"
    )
    # the agreeing neighbours are unchanged by the filter
    assert disp[0][0] == [2.0, 2.0], disp
    assert disp[2][2] == [2.0, 2.0], disp


# ---------------------------------------------------------------------------
# 6. ordering: reject THEN filter — a rejected cell is never resurrected
# ---------------------------------------------------------------------------


def test_a_rejected_cell_is_not_refilled_by_its_agreeing_neighbours():
    """Rejection comes first; the median runs over ACCEPTED points only.

    A whole-grid median would refill the rejected centre from its 8 agreeing neighbours, which
    would reintroduce the very bug the confidence gate exists to stop. The mesh's null action is
    zero displacement, so a rejected cell stays [0.0, 0.0].
    """
    controls = []
    for iy in range(3):
        for ix in range(3):
            if (ix, iy) == (1, 1):
                # low confidence: rejected, even though every neighbour agrees on [3.0, 3.0]
                controls.append(_control(ix, iy, dx=3.0, dy=3.0, error=0.99999))
            else:
                controls.append(_control(ix, iy, dx=3.0, dy=3.0, error=0.05))

    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )

    assert disp[1][1] == [0.0, 0.0], (
        f"a rejected control point must stay [0.0, 0.0] even when all 8 neighbours agree on "
        f"[3.0, 3.0]; got {disp[1][1]!r} — the median filter resurrected a rejected cell"
    )
    # the rejected cell must also be excluded from its neighbours' median windows, so the
    # neighbours keep their own agreed value rather than being dragged toward zero.
    assert disp[0][1] == [3.0, 3.0], (
        f"an accepted neighbour must not be dragged toward the rejected cell's [0.0, 0.0]; "
        f"got {disp[0][1]!r}"
    )


# ---------------------------------------------------------------------------
# 7. backward compatibility: a pre-change control JSON has no "error" key
# ---------------------------------------------------------------------------


def test_a_control_point_without_an_error_key_is_accepted_with_a_warning(caplog):
    """Accept-with-a-warning, so a resumed half-finished run does not lose every tile.

    Control JSONs written before this change carry no "error". Rejecting them would silently
    empty the mesh of an in-flight run; accepting them reproduces exactly the old behaviour for
    those tiles and says so in the log.
    """
    import logging

    controls = [_control(ix=0, iy=0, dx=4.0, dy=0.0, error=None)]
    assert "error" not in controls[0], "premise: the legacy control JSON has no error key"

    with caplog.at_level(logging.WARNING):
        _gx, _gy, disp = tiled_solve._grid_from_controls(
            controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
        )

    assert disp[0][0] == [4.0, 0.0], (
        f"a control point with no 'error' key must be accepted (unknown confidence, "
        f"pre-change JSON); got {disp[0][0]!r}"
    )
    assert any("error" in r.message.lower() for r in caplog.records), (
        f"accepting an unknown-confidence control point must be logged; "
        f"got records {[r.message for r in caplog.records]!r}"
    )


# ---------------------------------------------------------------------------
# 8. the manufactured all-zeros moving tile (bin/tiled_reg_tile.py:108)
# ---------------------------------------------------------------------------


def test_the_manufactured_zeros_moving_tile_is_rejected():
    """``tiled_reg_tile.py`` substitutes ``np.zeros`` when the moving crop is unavailable.

    It then emits a control point from it unconditionally. Correlating anything against an empty
    tile is degenerate — scikit-image cannot even compute an RMS error and returns NaN — so the
    gate must reject it rather than let a manufactured displacement into the mesh.
    """
    tiles = _tiles()
    ref, mov = tiles["tissue_vs_zeros"]
    assert not mov.any(), "premise: the manufactured moving tile is all zeros"

    dx, dy, tre, error = residual_displacement(ref, mov, upsample=10)
    ctx = f"manufactured zeros tile: dx={dx:.2f} dy={dy:.2f} |d|={tre:.2f} error={error!r}"
    assert not (error <= MAX_ERROR), (
        f"a degenerate correlation must not score as confident. {ctx}"
    )

    # force it past the lower TRE gate so the confidence gate is the only thing that can stop it
    controls = [_control(ix=0, iy=0, dx=8.0, dy=0.0, error=error)]
    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, gate_tre=GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )
    assert disp[0][0] == [0.0, 0.0], (
        f"the manufactured blank-vs-blank control point must be rejected; "
        f"got {disp[0][0]!r}. {ctx}"
    )
