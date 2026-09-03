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
    assert "error" not in controls[0], (
        "premise: the legacy control JSON has no error key"
    )

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


# ---------------------------------------------------------------------------
# 9. the SHIPPED default, read out of nextflow.config
# ---------------------------------------------------------------------------
#
# Everything above passes MAX_ERROR in as a kwarg, so none of it reads
# nextflow.config. That left exactly one tie between the shipped default and
# the empirical evidence -- an nf-test substring match -- and a substring match
# for "--max-error 0.99" is also satisfied by "0.9999", which re-admits
# background (measured 0.99947). This closes that loop: it reads the value the
# pipeline actually ships and asserts it still separates the measured tiles.
# Same shape as the repo's other config-reading guards (test_ci_stack_pinned.py,
# test_no_duplicate_param_defaults.py): plain parsing, no pipeline imports.

_CONFIG_PATH = os.path.join(_ROOT, "nextflow.config")
_PARAM_RE_TEMPLATE = r"^\s*{name}\s*=\s*([^\s/]+)"


def _shipped_param(name):
    """Read one parameter default out of nextflow.config's `params {}` block."""
    import re

    with open(_CONFIG_PATH) as fh:
        text = fh.read()
    start = text.find("params {")
    assert start != -1, "could not locate the `params {` block in nextflow.config"
    m = re.search(
        _PARAM_RE_TEMPLATE.format(name=re.escape(name)),
        text[start:],
        re.MULTILINE,
    )
    assert m, f"nextflow.config declares no `{name}` in its params block"
    return m.group(1)


def test_the_module_constant_matches_the_shipped_default():
    """The threshold every test above hardcodes must be the one the pipeline ships."""
    shipped = _shipped_param("reg_tiled_max_error")
    assert float(shipped) == MAX_ERROR, (
        f"nextflow.config ships reg_tiled_max_error = {shipped}, but this module's tests "
        f"are written against MAX_ERROR = {MAX_ERROR}. Re-measure before re-pinning: the "
        f"value has to sit between real tissue (measured 0.040956) and background "
        f"(measured 0.999469)."
    )


def test_the_shipped_max_error_default_actually_rejects_the_measured_garbage():
    """The shipped default, applied to the measured tiles, must accept tissue and reject garbage.

    This is the test that fails on a one-character config edit. Loosening
    reg_tiled_max_error to 0.9999 re-admits background (0.999469) and the section edge
    (0.999961) into the deformation mesh; tightening it below ~0.05 starts rejecting real
    tissue (0.040956).
    """
    shipped = float(_shipped_param("reg_tiled_max_error"))
    tiles = _tiles()

    verdicts = {}
    for name, (ref, mov) in tiles.items():
        dx, dy, tre, error = residual_displacement(ref, mov, upsample=10)
        # force every case past the lower TRE gate so the confidence gate is the only
        # thing that can decide, and keep |d| well inside the halo so the range gate
        # cannot mask a confidence-gate regression.
        control = _control(ix=0, iy=0, dx=8.0, dy=0.0, error=error)
        accepted, _reason = tiled_solve._accept(control, shipped, HALO)
        verdicts[name] = (error, accepted)

    table = "\n".join(
        f"    {n:16} error={e!r:12} {'ACCEPTED' if a else 'rejected'}"
        for n, (e, a) in verdicts.items()
    )
    ctx = f"shipped reg_tiled_max_error = {shipped}\n{table}"

    assert verdicts["tissue_4px"][1], f"the shipped default rejects REAL TISSUE:\n{ctx}"
    assert verdicts["tissue_0px"][1], (
        f"the shipped default rejects an aligned tile:\n{ctx}"
    )
    assert not verdicts["blank_vs_blank"][1], (
        f"the shipped default ADMITS BACKGROUND into the mesh:\n{ctx}"
    )
    assert not verdicts["tissue_vs_blank"][1], (
        f"the shipped default ADMITS A SECTION-EDGE TILE into the mesh:\n{ctx}"
    )
    assert not verdicts["tissue_vs_zeros"][1], (
        f"the shipped default admits the manufactured all-zeros tile:\n{ctx}"
    )


# ---------------------------------------------------------------------------
# 10. what fraction of a whole-slide grid this rejects -- MEASURED, not asserted in prose
# ---------------------------------------------------------------------------
#
# The headline number a human needs is "how much does this move registration
# output". It is construction-dependent: it turns on how a section-edge tile is
# modelled, and modelling one as tissue-vs-FULLY-blank is the easy case. Two
# reviewers building two different grids measured 73.0% and 50.5%.
#
# So these tests assert only the properties that are robust across constructions
# -- no pure-tissue tile rejected, every pure-background tile rejected, the range
# gate contributing nothing -- and PRINT the full breakdown so the number can be
# re-derived from the test rather than trusted from a report.

GRID_NY = GRID_NX = 20
# tissue ellipse -> 108 tiles (27%), a one-tile section-edge ring -> 72 (18%),
# pure background -> 220 (55%).
_ELLIPSE = (9.5, 9.5, 6.5, 5.5)  # cy, cx, ry, rx
# How much of a section-edge tile is off-section. THE HEADLINE REJECTION FRACTION TURNS ON
# THIS CHOICE and it is not a property of the fix: measured on this grid, a 50%-blanked edge
# model gives 55.0% overall and a 100%-blanked one gives 73.0%, because the edge tiles flip
# from accepted to rejected between them (see the band test). 0.50 and 1.00 are swept so that
# no assertion below can quietly depend on the easy end.
_EDGE_MODELS = (0.50, 1.00)


def _grid_tiles(rng, edge_blanked):
    """Yield (kind, ref, mov) for a synthetic whole-slide grid of stated composition."""
    field = _tissue_field(rng)

    def crop(dy, dx):
        return field[PAD + dy : PAD + dy + N, PAD + dx : PAD + dx + N].copy()

    def blank():
        return rng.normal(30.0, 3.0, (N, N))

    def partial(frac):
        t = crop(0, 4)
        cut = int(N * frac)
        if cut:
            t[:, :cut] = rng.normal(30.0, 3.0, (N, cut))
        return t

    cy0, cx0, ry, rx = _ELLIPSE
    for iy in range(GRID_NY):
        for ix in range(GRID_NX):
            r = ((iy - cy0) / ry) ** 2 + ((ix - cx0) / rx) ** 2
            if r <= 1.0:
                yield "tissue", ix, iy, crop(0, 0), crop(0, int(rng.integers(0, 6)))
            elif r <= 1.55:
                yield "edge", ix, iy, crop(0, 0), partial(edge_blanked)
            else:
                yield "background", ix, iy, blank(), blank()


@pytest.mark.parametrize("edge_blanked", _EDGE_MODELS)
def test_rejection_fraction_on_a_realistic_grid(edge_blanked):
    """Measure the rejection fraction and pin only what holds across edge models.

    The overall fraction is NOT a property of the fix -- it is 55.0% at a 50%-blanked edge
    model and 73.0% at a fully-blanked one, on this same grid. 73% is the BEST CASE, for the
    edge model that is easiest to reject. What is asserted here is what held in every model
    swept: no tissue tile rejected, every background tile rejected, no background displacement
    reaching the mesh, and the range gate contributing nothing.
    """
    rng = np.random.default_rng(SEED)
    controls, kinds = [], []
    for kind, ix, iy, ref, mov in _grid_tiles(rng, edge_blanked):
        dx, dy, tre, error = residual_displacement(ref, mov, upsample=10)
        controls.append(_control(ix, iy, dx, dy, error))
        kinds.append(kind)

    verdicts = [tiled_solve._accept(c, MAX_ERROR, HALO) for c in controls]
    total = len(controls)
    rejected = sum(1 for ok, _ in verdicts if not ok)
    by_gate = {"error": 0, "disp": 0}
    for ok, why in verdicts:
        if not ok:
            by_gate[why] += 1

    per_kind = {}
    for kind in ("tissue", "edge", "background"):
        n = sum(1 for k in kinds if k == kind)
        r = sum(1 for k, (ok, _) in zip(kinds, verdicts) if k == kind and not ok)
        per_kind[kind] = (n, r)

    # What the OLD code (TRE gate only) would have placed, vs what is placed now.
    placed_before = sum(1 for c in controls if float(c["tre"]) >= GATE_TRE)
    _gx, _gy, disp = tiled_solve._grid_from_controls(
        controls, GATE_TRE, max_error=MAX_ERROR, max_disp=HALO
    )
    placed_after = sum(1 for row in disp for d in row if d != [0.0, 0.0])
    garbage_before = sum(
        1
        for c, k in zip(controls, kinds)
        if k != "tissue" and float(c["tre"]) >= GATE_TRE
    )

    report = (
        f"\n  grid {GRID_NY}x{GRID_NX} = {total} tiles, "
        f"section-edge tiles {edge_blanked * 100:.0f}% blanked\n"
        + "".join(
            f"    {k:11} {per_kind[k][0]:4d} tiles  rejected {per_kind[k][1]:4d}  "
            f"({100.0 * per_kind[k][1] / per_kind[k][0]:5.1f}%)\n"
            for k in ("tissue", "edge", "background")
        )
        + f"  OVERALL rejected {rejected}/{total} = {100.0 * rejected / total:.1f}%\n"
        f"    by confidence gate {by_gate['error']}   by range gate {by_gate['disp']}\n"
        f"  control points placed in the mesh: before {placed_before}/{total}, "
        f"after {placed_after}/{total}\n"
        f"    of the {placed_before} placed before, {garbage_before} were "
        f"background/section-edge garbage\n"
        "  NOTE: the OVERALL fraction depends on grid composition -- see\n"
        "  test_the_partial_tissue_band_is_where_the_gate_stops_being_decisive.\n"
    )

    # --- the robust properties, the ones that held across every construction tried ---
    assert per_kind["tissue"][1] == 0, f"real tissue is being rejected:{report}"
    assert per_kind["background"][0] == per_kind["background"][1], (
        f"pure background is reaching the mesh:{report}"
    )
    assert by_gate["disp"] == 0, (
        f"the range gate fired on this grid; it caught nothing in every construction "
        f"measured, so the confidence gate is what is load-bearing:{report}"
    )
    assert by_gate["error"] == rejected, f"a rejection came from neither gate:{report}"
    assert placed_after < placed_before, f"the gates placed no fewer points:{report}"
    # No BACKGROUND displacement may reach the mesh. This is the robust form of the claim.
    # (An earlier version asserted `placed_kinds == ["tissue"]`, which held only at the pinned
    # 0.75 edge model -- at 0.50/0.60/0.65 the section-edge tiles are accepted and the
    # assertion fails with "carries displacements from ['edge', 'tissue']". Section-edge tiles
    # reaching the mesh IS the known exposure the band test pins; it is not a background leak,
    # and the assertion must distinguish the two rather than accidentally forbid both.)
    kind_at = {(c["iy"], c["ix"]): k for c, k in zip(controls, kinds)}
    placed_kinds = sorted(
        {
            kind_at[(iy, ix)]
            for iy, row in enumerate(disp)
            for ix, d in enumerate(row)
            if d != [0.0, 0.0]
        }
    )
    assert "background" not in placed_kinds, (
        f"a pure-background displacement reached the mesh; placed kinds "
        f"{placed_kinds}:{report}"
    )
    # --- the overall fraction, asserted only as a broad range (it is construction-dependent) ---
    frac = rejected / total
    assert 0.40 <= frac <= 0.90, (
        f"overall rejection fraction {frac:.3f} outside the broad expected band "
        f"[0.40, 0.90]. Measured on this grid: 0.550 at a 50%-blanked edge model, 0.730 at a "
        f"fully-blanked one:{report}"
    )


BAND_GEOMETRIES = ("left", "right", "top", "bottom")
BAND_SEEDS = (1234, 7, 99)
BAND_FRACTIONS = tuple(round(i * 0.05, 2) for i in range(21))


def _blanked_tile(seed, blanked, geom):
    """A tile with `blanked` of its area replaced by background, in one of four geometries."""
    rng = np.random.default_rng(seed)
    field = _tissue_field(rng)
    ref = field[PAD : PAD + N, PAD : PAD + N].copy()
    mov = field[PAD : PAD + N, PAD + 4 : PAD + 4 + N].copy()
    cut = int(N * blanked)
    if cut:
        bg = rng.normal(30.0, 3.0, (N, N))
        if geom == "left":
            mov[:, :cut] = bg[:, :cut]
        elif geom == "right":
            mov[:, N - cut :] = bg[:, N - cut :]
        elif geom == "top":
            mov[:cut, :] = bg[:cut, :]
        elif geom == "bottom":
            mov[N - cut :, :] = bg[N - cut :, :]
        else:
            raise AssertionError(f"unknown geometry {geom!r}")
    return ref, mov


def _sweep_band(seed, geom):
    """Return [(blanked, dx, dy, tre, error, accepted, shift_correct)] over the dense band."""
    rows = []
    for blanked in BAND_FRACTIONS:
        ref, mov = _blanked_tile(seed, blanked, geom)
        dx, dy, tre, error = residual_displacement(ref, mov, upsample=10)
        accepted, _reason = tiled_solve._accept(
            _control(0, 0, dx, dy, error), MAX_ERROR, HALO
        )
        rows.append(
            (
                blanked,
                dx,
                dy,
                tre,
                error,
                accepted,
                abs(dx - 4.0) < 0.6 and abs(dy) < 0.6,
            )
        )
    return rows


def _band_table(seed, geom, rows):
    head = f"\n  seed={seed} geometry={geom}  (true shift is dx=4.00, dy=0.00)\n"
    return head + "".join(
        f"    blanked {b * 100:5.0f}%  dx={dx:8.2f} dy={dy:8.2f} |d|={tre:7.2f} "
        f"error={err:.4f}  {'ACCEPTED' if a else 'rejected':8}  "
        f"shift {'recovered' if c else 'LOST'}"
        f"{'   <== ACCEPTED WITH A WRONG SHIFT' if (a and not c) else ''}\n"
        for b, dx, dy, tre, err, a, c in rows
    )


# Measured bounds of the three regions, across all BAND_SEEDS x BAND_GEOMETRIES configs.
# Deliberately loose: the exact flip points move with seed and geometry (the low region ends
# anywhere from 35% to 55% blanked depending on the config, and the all-rejected region starts
# anywhere from 70% to 95%), so these are the min/max envelope, not a single config's boundary.
BAND_ALWAYS_GOOD_MAX = 0.30  # measured: every config still accepted+correct through 35%
BAND_ALWAYS_REJECTED_MIN = 0.95  # measured: every config rejects everything from 95% on


@pytest.mark.parametrize("geom", BAND_GEOMETRIES)
@pytest.mark.parametrize("seed", BAND_SEEDS)
def test_the_partial_tissue_band_has_a_documented_accept_but_wrong_region(seed, geom):
    """Sweep how much of a tile is off-section and pin the SHAPE the gate actually has.

    The confidence gate is decisive at both ends of this band and NOT in the middle. Three
    regions, all measured over 21 blanking fractions x 4 geometries x 3 seeds:

    1. **low blanking** (through 30%, measured 35%): accepted, and the true shift is recovered.
    2. **middle**: the tile is ACCEPTED AND THE RECOVERED SHIFT IS WRONG -- between 4 and 9 of
       the 21 sampled fractions in *every* config measured, injecting a bogus displacement of
       up to 131 px. This is a KNOWN, UNCLOSED EXPOSURE, asserted here rather than avoided:
       foreground/Otsu detection is the real fix and is deliberately out of scope
       (see the note at ``_accept`` in bin/tiled_solve.py).
    3. **high blanking** (from 95%): rejected.

    An earlier version of this test parametrised [0.0, 0.25, 0.5, 0.75, 1.0] and passed, which
    is why it now sweeps densely: those five points step straight over region 2, and the test
    reported a boundary the gate does not have. A test that passes because of which points were
    sampled is worse than no test.
    """
    rows = _sweep_band(seed, geom)
    table = _band_table(seed, geom, rows)

    low = [r for r in rows if r[0] <= BAND_ALWAYS_GOOD_MAX]
    high = [r for r in rows if r[0] >= BAND_ALWAYS_REJECTED_MIN]
    accepted_but_wrong = [r for r in rows if r[5] and not r[6]]

    assert low and high, f"the sweep did not cover both ends of the band:{table}"

    # 1. the low region: the gate must not throw away tiles it can register correctly
    for b, dx, dy, tre, err, a, c in low:
        assert a and c, (
            f"a tile only {b * 100:.0f}% blanked was not accepted-and-correct; the confidence "
            f"gate is rejecting registrable tissue:{table}"
        )

    # 2. the middle region: the known exposure. Asserted to EXIST so it cannot be quietly
    #    forgotten, and so that anyone who closes it (with foreground detection) is forced to
    #    come here, delete this assertion and update the docstring.
    assert accepted_but_wrong, (
        f"no accepted-but-wrong tile found in this sweep. If the gate genuinely no longer has "
        f"this exposure, that is GOOD NEWS and this assertion (and the limitation note at "
        f"_accept in bin/tiled_solve.py) must be updated -- do not simply delete it:{table}"
    )
    worst = max(r[3] for r in accepted_but_wrong)
    assert worst > 20.0, (
        f"the accepted-but-wrong displacements are all under 20 px ({worst:.1f} px worst), "
        f"which would make this exposure immaterial; re-characterise it:{table}"
    )
    # and the range gate cannot save us here -- that is why it is not the load-bearing gate
    assert worst < HALO, (
        f"worst accepted-but-wrong |d| = {worst:.1f} px is at/over the halo ({HALO}), so the "
        f"range gate would have caught it; the exposure would then not be what this pins:{table}"
    )

    # 3. the high region: a tile that is essentially all background must never get through
    for b, dx, dy, tre, err, a, c in high:
        assert not a, (
            f"a tile {b * 100:.0f}% blanked was ACCEPTED; near-empty tiles must be "
            f"rejected:{table}"
        )
