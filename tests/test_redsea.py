"""Unit tests for the REDSEA reimplementation (bin/utils/redsea.py).

The reference is the published MATLAB
(github.com/nolanlab/REDSEA, ``MIBIboundary_compensation_boundarySA.m``). The
tests below pin the three things a port can silently get wrong:

1. the shared-perimeter matrix, on BOTH mask conventions (zero-walled, as the
   original assumes, and touching, as every modern segmenter emits),
2. the compensation algebra, against a hand-computed two-cell case,
3. the tiling, which must be a pure implementation detail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin" / "utils"))

import redsea  # noqa: E402


def _touching_pair(h=10, w=20):
    """Two 10x10 cells sharing a vertical interface, no wall between them."""
    m = np.zeros((h, w), dtype=np.int32)
    m[:, : w // 2] = 1
    m[:, w // 2 :] = 2
    return m


def _walled_pair(h=10, w=21):
    """Same two cells, separated by the one-pixel zero wall the original assumes."""
    m = np.zeros((h, w), dtype=np.int32)
    m[:, : w // 2] = 1
    m[:, w // 2 + 1 :] = 2
    return m


# ── shared-perimeter matrix ───────────────────────────────────────────────────
def test_touching_mask_yields_full_reciprocal_perimeter():
    """Two cells that touch only each other each give 100% of their perimeter."""
    w, neighbourless = redsea.shared_boundary_matrix(_touching_pair())
    assert neighbourless == 0
    assert w[1, 2] == pytest.approx(1.0)
    assert w[2, 1] == pytest.approx(1.0)
    assert w[1, 1] == 0.0


def test_walled_mask_is_counted_too():
    """The original's convention still works: a zero wall is not a missing neighbour.

    This is the regression that breaks a literal port in the other direction — a
    direct-contact-only implementation reports every watershed cell as isolated.
    """
    w, neighbourless = redsea.shared_boundary_matrix(_walled_pair())
    assert neighbourless == 0
    assert w[1, 2] == pytest.approx(1.0)
    assert w[2, 1] == pytest.approx(1.0)


def test_isolated_cell_is_zero_not_nan():
    """The original divides by a zero perimeter and NaNs the whole image.

    ``cellPairNorm = R*eye - cellPairMap ./ cellBoundaryTotalMatrix`` in
    ``MIBIboundary_compensation_boundarySA.m`` has no guard, so one isolated cell
    puts NaN in a row of the matrix and the mat-mul spreads it to every cell.
    """
    m = np.zeros((20, 20), dtype=np.int32)
    m[2:6, 2:6] = 1  # alone, surrounded by background
    w, neighbourless = redsea.shared_boundary_matrix(m)
    assert neighbourless == 1
    assert np.isfinite(w.toarray()).all()
    assert w.toarray().sum() == 0.0


def test_shared_fractions_sum_to_one_per_cell():
    """Row sums are 1 for every cell that has at least one neighbour."""
    rng = np.random.default_rng(0)
    m = rng.integers(1, 6, size=(40, 40)).astype(np.int32)
    w, _ = redsea.shared_boundary_matrix(m)
    rows = np.asarray(w.sum(axis=1)).ravel()
    assert rows[1:6] == pytest.approx(np.ones(5))


def test_three_cell_junction_is_pairwise():
    """A three-way meeting contributes to all three pairs, as nchoosek does."""
    m = np.zeros((12, 12), dtype=np.int32)
    m[:6, :6] = 1
    m[:6, 6:] = 2
    m[6:, :] = 3
    w, _ = redsea.shared_boundary_matrix(m)
    for a, b in ((1, 2), (1, 3), (2, 3)):
        assert w[a, b] > 0, f"pair {a},{b} missing"


# ── boundary band ─────────────────────────────────────────────────────────────
def test_band_size_one_is_the_bare_rim():
    m = _touching_pair()
    band = redsea.boundary_band(m, element_size=1)
    # Every cell pixel on an image edge or at the 1/2 interface is rim.
    assert band[:, 9].all() and band[:, 10].all()  # the interface
    assert not band[5, 5]  # deep interior of cell 1


def test_band_deepens_monotonically():
    m = np.zeros((60, 60), dtype=np.int32)
    yy, xx = np.ogrid[:60, :60]
    m[(yy - 30) ** 2 + (xx - 30) ** 2 <= 20**2] = 1
    prev = 0
    for size in (1, 3, 6, 10):
        n = int(redsea.boundary_band(m, size).sum())
        assert n > prev, f"band did not grow at element_size={size}"
        prev = n


def test_band_never_leaves_the_cells():
    m = _walled_pair()
    band = redsea.boundary_band(m, element_size=3)
    assert not band[m == 0].any()


def test_element_shape_changes_the_band():
    m = np.zeros((60, 60), dtype=np.int32)
    m[10:50, 10:50] = 1
    sizes = {
        s: int(redsea.boundary_band(m, 6, element_shape=s).sum())
        for s in ("square", "diamond", "disk")
    }
    # Chebyshev reaches furthest per step, taxicab least; disk in between.
    assert sizes["diamond"] <= sizes["disk"] <= sizes["square"]


def test_unknown_element_shape_raises():
    with pytest.raises(ValueError, match="element_shape"):
        redsea.boundary_band(_touching_pair(), 2, element_shape="star")


# ── the compensation algebra ──────────────────────────────────────────────────
def test_two_cell_compensation_matches_hand_calculation():
    """Hand-check of ``compensated = raw + R*edge - W.T @ edge``.

    Cells 1 and 2 share their entire perimeter, so W[1,2] = W[2,1] = 1. With
    R = 1 the compensated value of cell 1 is raw1 + edge1 - edge2.
    """
    w, _ = redsea.shared_boundary_matrix(_touching_pair())
    raw = np.array([0.0, 100.0, 40.0])
    edge = np.array([0.0, 30.0, 10.0])
    out = redsea.compensate(raw, edge, w, redsea_checker=1)
    assert out[1] == pytest.approx(100 + 30 - 10)
    assert out[2] == pytest.approx(40 + 10 - 30)


def test_checker_zero_is_subtraction_only():
    w, _ = redsea.shared_boundary_matrix(_touching_pair())
    raw = np.array([0.0, 100.0, 40.0])
    edge = np.array([0.0, 30.0, 10.0])
    out = redsea.compensate(raw, edge, w, redsea_checker=0)
    assert out[1] == pytest.approx(100 - 10)
    assert out[2] == pytest.approx(40 - 30)


def test_compensation_clips_at_zero():
    """The original's ``MIBIdataNorm2(MIBIdataNorm2<0) = 0``."""
    w, _ = redsea.shared_boundary_matrix(_touching_pair())
    raw = np.array([0.0, 1.0, 500.0])
    edge = np.array([0.0, 0.0, 500.0])
    out = redsea.compensate(raw, edge, w, redsea_checker=0)
    assert out[1] == 0.0


def test_isolated_cell_passes_through_unchanged():
    m = np.zeros((20, 20), dtype=np.int32)
    m[2:6, 2:6] = 1
    w, _ = redsea.shared_boundary_matrix(m)
    raw = np.array([0.0, 77.0])
    edge = np.array([0.0, 20.0])
    out = redsea.compensate(raw, edge, w, redsea_checker=0)
    assert out[1] == pytest.approx(77.0)


def test_compensation_rejects_bad_checker():
    w, _ = redsea.shared_boundary_matrix(_touching_pair())
    with pytest.raises(ValueError, match="redsea_checker"):
        redsea.compensate(np.zeros(3), np.zeros(3), w, redsea_checker=2)


def test_spillover_round_trip_recovers_the_truth():
    """End-to-end: synthesise a known leak, then check REDSEA removes most of it.

    Cell 1 is the only source of the marker. A fixed fraction of its membrane
    signal is painted into cell 2's band. Un-compensated, cell 2 looks positive;
    compensated, it should come back near zero while cell 1 keeps its signal.
    """
    mask = _touching_pair(h=40, w=40)
    geom = redsea.redsea_geometry(mask, element_size=4)
    truth = np.zeros(mask.shape, dtype=np.float64)
    truth[(mask == 1) & geom.band] = 10.0  # cell 1's membrane
    leaked = truth.copy()
    # 40% of the membrane signal lands one band's width over the interface
    interface = geom.band & (mask == 2)
    leaked[interface] = 4.0
    whole, edge = redsea.band_sums(mask, geom.band, leaked)
    out = redsea.compensate(whole, edge, geom.weights, redsea_checker=1)
    assert out[2] < whole[2] * 0.5, "spillover into cell 2 was not reduced"
    assert out[1] > whole[1] * 0.8, "cell 1 lost its own signal"


# ── geometry bundle + tiling ──────────────────────────────────────────────────
def test_geometry_reports_band_fraction():
    m = _touching_pair(h=40, w=40)
    geom = redsea.redsea_geometry(m, element_size=3)
    assert list(geom.labels) == [1, 2]
    assert geom.band_fraction.shape == (2,)
    assert (geom.band_fraction > 0).all()
    assert (geom.band_fraction <= 1).all()


def test_band_fraction_saturates_when_element_size_swallows_the_cell():
    """The diagnostic the REDSEApy author calls out: cells that are all border.

    At an element_size comparable to the cell radius the band is the whole cell
    and boundary mode silently degrades into whole-cell mode. This is what makes
    the reported band fraction worth publishing rather than an internal detail.
    """
    m = _touching_pair(h=40, w=40)
    assert redsea.redsea_geometry(m, element_size=30).band_fraction.min() == 1.0


def test_geometry_rejects_3d_masks():
    with pytest.raises(ValueError, match="2D"):
        redsea.redsea_geometry(np.zeros((2, 4, 4), dtype=np.int32), 2)


def test_tiling_matches_untiled():
    """Row-blocking is an implementation detail and must not change any result.

    Every tiled pass re-reads a halo; if a halo is wrong the error shows up only
    at a block seam, which is exactly the kind of bug a single-block test misses.
    """
    rng = np.random.default_rng(7)
    m = rng.integers(0, 8, size=(97, 61)).astype(np.int32)
    big = redsea.shared_boundary_matrix(m, block_rows=10_000)[0].toarray()
    small = redsea.shared_boundary_matrix(m, block_rows=7)[0].toarray()
    assert np.allclose(big, small)

    band_big = redsea.boundary_band(m, 3, block_rows=10_000)
    band_small = redsea.boundary_band(m, 3, block_rows=7)
    assert np.array_equal(band_big, band_small)


# ── the parameter rule ────────────────────────────────────────────────────────
def test_recommend_element_size_reproduces_the_published_settings():
    """The rule is calibrated so it returns the paper's own two numbers.

    MIBI: 512 px per 400 um -> 0.781 um/px; mean cell 107 px -> 11.7 px across;
    published elementSize 2.  CyCIF: mean cell 325 px -> 20.3 px across;
    published elementSize 3-4.
    """
    assert redsea.recommend_element_size(11.67 * 0.781, 0.781) == 2
    cycif_px = 0.5
    assert redsea.recommend_element_size(20.3 * cycif_px, cycif_px) in (3, 4)


def test_recommend_element_size_for_this_pipelines_optics():
    """0.325 um/px, ~20 um cells -> ~11 px, not the literal MIBI 2."""
    assert redsea.recommend_element_size(20.0, 0.325) == 11


def test_recommend_element_size_rejects_nonsense():
    with pytest.raises(ValueError):
        redsea.recommend_element_size(20.0, 0.0)
    with pytest.raises(ValueError):
        redsea.recommend_element_size(0.0, 0.325)


# ── equivalence with the published MATLAB ─────────────────────────────────────
def _matlab_boundarysa_reference(newLmod, data, edge, redsea_checker):
    """Literal transcription of ``MIBIboundary_compensation_boundarySA.m``.

    Dense and scalar, exactly as published — that is the point. It is the oracle
    the vectorised/sparse implementation is checked against, so "we made it fast"
    is a claim with a test behind it rather than a comment. Only tractable on toy
    masks, which is also exactly why the original cannot be used as-is: its
    ``zeros(cellNum, cellNum)`` is 2 TB at 5e5 cells.

    ``edge`` is passed in rather than recomputed, so this pins the *algebra*
    (adjacency, normalisation, mat-mul, clip). The band itself differs on purpose
    and is documented in ``redsea.boundary_band``.
    """
    from itertools import combinations

    cell_num = int(newLmod.max())
    pair = np.zeros((cell_num + 1, cell_num + 1))
    padded = np.pad(newLmod, 1, constant_values=0)
    for x in range(1, padded.shape[0] - 1):
        for y in range(1, padded.shape[1] - 1):
            if padded[x, y] != 0:
                continue
            factors = np.unique(padded[x - 1 : x + 2, y - 1 : y + 2])
            if len(factors) >= 3:
                for a, b in combinations(factors[1:], 2):
                    pair[a, b] += 1
    pair = pair + pair.T
    total = pair.sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        norm = redsea_checker * np.eye(cell_num + 1) - pair / total[:, None]
    norm = np.nan_to_num(norm, nan=0.0, posinf=0.0, neginf=0.0)
    # MATLAB: MIBIdataNorm2 = (MIBIdataNearEdge1' * cellPairNorm)' + data
    out = norm.T @ edge + data
    return np.clip(out, 0.0, None)


@pytest.mark.parametrize("redsea_checker", [0, 1])
def test_matches_published_matlab_on_a_walled_mask(redsea_checker):
    """The vectorised/sparse path reproduces the published dense/scalar one.

    A walled mask is used because that is the only convention the original
    handles; the touching-mask branch has no MATLAB counterpart to compare to.
    """
    rng = np.random.default_rng(11)
    # A walled mask: grow labelled seeds, then knock out the pixels where two
    # labels meet so a one-pixel zero wall separates every cell, MATLAB-style.
    from scipy import ndimage as ndi

    seeds = np.zeros((45, 45), dtype=np.int32)
    pts = rng.choice(45 * 45, size=12, replace=False)
    seeds.ravel()[pts] = np.arange(1, 13)
    _, idx = ndi.distance_transform_edt(seeds == 0, return_indices=True)
    grown = seeds[tuple(idx)]
    rim = np.zeros_like(grown, dtype=bool)
    rim[:-1, :] |= grown[:-1, :] != grown[1:, :]
    rim[:, :-1] |= grown[:, :-1] != grown[:, 1:]
    walled = np.where(rim, 0, grown).astype(np.int32)

    n = int(walled.max())
    data = rng.random(n + 1) * 500
    edge = rng.random(n + 1) * 150
    data[0] = edge[0] = 0.0

    expected = _matlab_boundarysa_reference(walled, data, edge, redsea_checker)
    weights, _ = redsea.shared_boundary_matrix(walled)
    got = redsea.compensate(data, edge, weights, redsea_checker=redsea_checker)

    assert got == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_matlab_reference_would_nan_where_this_does_not():
    """Proof the isolated-cell guard is fixing something real, not hypothetical.

    Without ``np.nan_to_num`` the transcription above produces NaN for the whole
    image off a single neighbourless cell. That is the published behaviour.
    """
    from itertools import combinations

    m = np.zeros((20, 20), dtype=np.int32)
    m[2:6, 2:6] = 1
    m[10:14, 10:14] = 2
    cell_num = 2
    pair = np.zeros((cell_num + 1, cell_num + 1))
    padded = np.pad(m, 1, constant_values=0)
    for x in range(1, padded.shape[0] - 1):
        for y in range(1, padded.shape[1] - 1):
            if padded[x, y] == 0:
                f = np.unique(padded[x - 1 : x + 2, y - 1 : y + 2])
                if len(f) >= 3:
                    for a, b in combinations(f[1:], 2):
                        pair[a, b] += 1
    pair = pair + pair.T
    with np.errstate(divide="ignore", invalid="ignore"):
        unguarded = np.eye(cell_num + 1) - pair / pair.sum(axis=0)[:, None]
    assert np.isnan(unguarded).any(), "the reference should NaN here"

    weights, neighbourless = redsea.shared_boundary_matrix(m)
    assert neighbourless == 2
    assert np.isfinite(weights.toarray()).all()


# ── data-driven calibration ───────────────────────────────────────────────────
def _voronoi_mask(n_cells=60, size=400, seed=3):
    """Realistically-shaped touching cells: grown seeds, no zero wall.

    Deliberately NOT disks — the whole point of calibration is that the paper's
    closed-form rule is disk geometry and real cells are not.
    """
    from scipy import ndimage as ndi

    rng = np.random.default_rng(seed)
    seeds = np.zeros((size, size), dtype=np.int32)
    pts = rng.choice(size * size, size=n_cells, replace=False)
    seeds.ravel()[pts] = np.arange(1, n_cells + 1)
    _, idx = ndi.distance_transform_edt(seeds == 0, return_indices=True)
    return seeds[tuple(idx)].astype(np.int32)


def test_calibration_hits_the_target_band_fraction():
    m = _voronoi_mask()
    best, table = redsea.calibrate_element_size(m, target_fraction=0.56)
    assert abs(table[best] - 0.56) <= 0.05
    # and no other candidate is closer — that is the whole contract
    assert all(abs(table[best] - 0.56) <= abs(v - 0.56) for v in table.values())


def test_calibration_table_is_monotonic():
    """A deeper band can only cover more of the cell."""
    _, table = redsea.calibrate_element_size(_voronoi_mask(), max_size=15)
    vals = [table[s] for s in sorted(table)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_calibration_disagrees_with_the_closed_form_rule_on_real_shapes():
    """The reason calibration exists, pinned as a test.

    ~58 px cells: the disk formula says 11 px, the mask says ~7. If a future
    change makes these agree, the rule changed and the docs are wrong.
    """
    m = _voronoi_mask()
    areas = np.bincount(m.ravel())[1:]
    diameter_px = 2 * np.sqrt(areas.mean() / np.pi)
    formula = redsea.recommend_element_size(diameter_px * 0.325, 0.325)
    calibrated, table = redsea.calibrate_element_size(m, target_fraction=0.56)
    assert calibrated < formula
    assert table[formula] > 0.65, "the formula should over-shoot here"


def test_calibration_matches_boundary_band():
    """Calibration must measure the same band the run will actually use."""
    m = _voronoi_mask(n_cells=20, size=120, seed=9)
    _, table = redsea.calibrate_element_size(m, max_size=6)
    geom = redsea.redsea_geometry(m, element_size=4)
    assert geom.band_fraction.mean() == pytest.approx(table[4], abs=1e-9)


def test_calibration_rejects_impossible_targets():
    with pytest.raises(ValueError, match="target_fraction"):
        redsea.calibrate_element_size(_touching_pair(), target_fraction=1.5)


# ── on-disk round trip ────────────────────────────────────────────────────────
def test_geometry_round_trips_through_the_npz(tmp_path):
    """The bit-packed band and the CSR triple must survive the file.

    ``bin/redsea_matrix.py`` writes this and every one of the patient's per-marker
    QUANTIFY tasks reads it, so a silent packing bug would corrupt every marker
    at once with no error.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
    import redsea_matrix

    m = _voronoi_mask(n_cells=15, size=100, seed=5)
    geom = redsea.redsea_geometry(m, element_size=4)
    out = tmp_path / "g.npz"
    redsea_matrix.save_geometry(geom, out)
    w, band, labels = redsea_matrix.load_geometry(str(out))

    assert np.array_equal(band, geom.band)
    assert np.array_equal(labels, geom.labels)
    assert w.shape == geom.weights.shape
    assert np.allclose(w.toarray(), geom.weights.toarray(), atol=1e-6)
