"""REDSEA lateral-spillover compensation — the algorithm, reimplemented.

Port of REDSEA (Bai et al., *Front. Immunol.* 2021, 12:652631;
https://github.com/nolanlab/REDSEA) from MATLAB to vectorised NumPy/SciPy.

WHY A REIMPLEMENTATION AND NOT A TRANSLATION
--------------------------------------------
The published MATLAB is O(n_cells^2) in *memory* and O(n_pixels) in interpreted
loops. ``MIBIboundary_compensation_boundarySA.m`` allocates ``cellPairMap =
zeros(cellNum, cellNum)`` — a dense cell-by-cell matrix. That is fine for a MIBI
field of view (~1-2 k cells, ~30 MB) and impossible for a WSI: at 5e5 cells the
same matrix is 2 TB. It then walks every pixel in a scalar ``for x / for y``
loop, and walks every cell's pixel list again in a second scalar loop.

The *algorithm*, though, is cheap. Its two real costs are
(1) one pass over the mask to find which cells touch and how much, and
(2) one pass per channel to sum that channel over each cell's boundary band.
Both are O(n_pixels), i.e. the same order as the quantification Mirage already
does. The cell-by-cell matrix is intrinsically sparse — a cell touches ~6
neighbours — so ``scipy.sparse`` removes the quadratic term entirely.

Everything below is therefore mathematically the published method with the
dense/scalar parts replaced. The one deliberate numerical change is the boundary
band (see :func:`boundary_band`), and it is documented there.

THE METHOD, IN THE PAPER'S OWN TERMS
------------------------------------
For adjacent cells A and B, some of A's membrane signal lands inside B's mask.
REDSEA estimates that leak from the *boundary band* signal and the *shared
perimeter fraction*, then subtracts it and (optionally) hands the same quantity
back to the cell it came from:

    compensated_B = raw_B
                    + R * edge_B                       (reinforcement)
                    - sum_over_neighbours_A edge_A * shared(A,B) / perimeter(A)

where ``edge_X`` is X's signal summed over its own boundary band, ``shared(A,B)``
is the length of the A/B interface, ``perimeter(A)`` is the total length of A's
interface with *other cells* (background-facing perimeter is excluded — that is
the original's choice and is preserved), and ``R`` is ``REDSEAChecker``
(0 = subtract only, 1 = subtract and reinforce; the paper's default is 1).

Negative results are clipped to zero, as in the original.

Written as matrices with ``W[a, b] = shared(a, b) / perimeter(a)``:

    compensated = raw + R * edge - W.T @ edge

which is one sparse mat-vec per channel — ``nnz(W) ~ 6 * n_cells``.

**REDSEA is separable across channels.** The mixing in ``W`` is between *cells*,
never between channels, so each marker can be compensated on its own as long as
every cell's boundary-band sum for that marker is available. That is what lets
Mirage apply it inside its existing per-channel ``QUANTIFY`` fan-out: the
per-patient part (this module's :func:`redsea_geometry`) runs once, and the
per-channel part (:func:`compensate`) runs in parallel, one task per marker.

**REDSEA compensates sums, not medians.** The subtraction is of integrated
counts; a median has no such algebra. So the compensated statistics are Sum and
its area-normalised partner Mean, and Mirage's default Median statistic is left
untouched and un-compensated.

WHAT IT CANNOT DO (from the paper's own limitations section)
------------------------------------------------------------
No 3D correction; cannot separate physically overlapping cells; does not touch
autofluorescence, spectral bleed-through or isotopic contamination. It assumes
the marker is roughly uniform around the source cell's membrane and brighter
inside the source than in the leak — i.e. it is for **surface/membrane markers**.
Applying it to a nuclear or diffuse-cytoplasmic marker violates the assumption,
which is why the marker list is opt-in rather than "all channels".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
import scipy.sparse as sp
from numpy.typing import NDArray
from scipy import ndimage

__all__ = [
    "ELEMENT_SHAPES",
    "RedseaGeometry",
    "boundary_band",
    "calibrate_element_size",
    "compensate",
    "recommend_element_size",
    "redsea_geometry",
    "shared_boundary_matrix",
]

# elementShape, keeping the original's integer codes where they exist.
#   1 -> square   : MATLAB ``strel('square', 2*elementSize+1)``, Chebyshev metric
#   2 -> diamond  : MATLAB ``strel('diamond', elementSize)``,    taxicab metric
#   3 -> disk     : Euclidean. Not in the original, and the default here — see
#                   ``boundary_band`` for why it matters once elementSize is large.
ELEMENT_SHAPES: Dict[str, str] = {
    "square": "chessboard",
    "diamond": "taxicab",
    "disk": "euclidean",
}

# Row-block height for the tiled passes. Chosen so a block's working set stays in
# the hundreds of MB for a WSI-width mask rather than tens of GB; every tiled pass
# re-reads a `halo` of rows on each side so results are identical to the untiled
# computation (asserted by tests/test_redsea.py::test_tiling_matches_untiled).
DEFAULT_BLOCK_ROWS = 4096


@dataclass(frozen=True)
class RedseaGeometry:
    """The per-patient, channel-independent half of REDSEA.

    Attributes
    ----------
    weights : scipy.sparse.csr_matrix, shape (n+1, n+1)
        ``W[a, b] = shared(a, b) / perimeter(a)`` — the fraction of cell ``a``'s
        cell-facing perimeter that it shares with cell ``b``. Indexed by *label*,
        so row/column 0 exists and is empty (labels are 1-based). Rows for cells
        with no neighbour are all-zero rather than NaN (see
        :func:`shared_boundary_matrix`).
    band : ndarray of bool, shape (Y, X)
        The boundary band: cell pixels within ``element_size`` of their own cell's
        edge.
    labels : ndarray of int
        Sorted non-zero cell labels present in the mask.
    band_fraction : ndarray of float
        Per-cell ``band_pixels / cell_pixels``, aligned with ``labels``. The
        diagnostic the REDSEApy port's author flags as the one to watch: if this
        approaches 1.0 the "band" is the whole cell and the boundary mode degrades
        into the whole-cell mode. See :func:`recommend_element_size`.
    n_neighbourless : int
        Cells with no touching neighbour. Their signal passes through unchanged.
    """

    weights: sp.csr_matrix
    band: NDArray[np.bool_]
    labels: NDArray[np.integer]
    band_fraction: NDArray[np.floating]
    n_neighbourless: int


def _neighbour_pairs_direct(
    mask: NDArray, block_rows: int = DEFAULT_BLOCK_ROWS
) -> Tuple[NDArray, NDArray]:
    """Interface length between labels that touch *directly* (no gap).

    This is the modern-segmenter case: Mesmer/DeepCell, StarDist, CellSAM and
    InstantSeg all emit masks whose cells abut with no separating zero, so the
    original's "scan the pixels labelled 0" rule finds nothing at all and every
    perimeter comes out as 0. The REDSEApy port hit exactly this and worked
    around it; here it is a first-class branch.

    Counts 4-connected adjacent pixel pairs carrying two different non-zero
    labels. Returns the two label vectors (unordered pairs, unsorted, with
    duplicates — the caller sums them).
    """
    rows, cols = [], []
    n_rows = mask.shape[0]

    def _emit(a, b):
        if a.size == 0:
            return
        sel = (a != b) & (a > 0) & (b > 0)
        if sel.any():
            rows.append(a[sel])
            cols.append(b[sel])

    for start in range(0, n_rows, block_rows):
        stop = min(start + block_rows, n_rows)
        # Vertical pairs need one extra row so the pair straddling a block edge is
        # counted -- once, in the earlier block. Horizontal pairs must NOT see that
        # extra row, or every seam row is counted twice (caught by
        # tests/test_redsea.py::test_tiling_matches_untiled).
        vblk = mask[start : min(stop + 1, n_rows)]
        _emit(vblk[:-1, :], vblk[1:, :])
        hblk = mask[start:stop]
        _emit(hblk[:, :-1], hblk[:, 1:])
    if not rows:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty
    return (
        np.concatenate(rows).astype(np.int64, copy=False),
        np.concatenate(cols).astype(np.int64, copy=False),
    )


def _neighbour_pairs_walled(
    mask: NDArray, block_rows: int = DEFAULT_BLOCK_ROWS
) -> Tuple[NDArray, NDArray]:
    """Interface length between labels separated by a one-pixel zero wall.

    This is the original's rule, and the shape of a classic watershed
    ``segmentationParams.mat``: at every pixel labelled 0, take the distinct
    non-zero labels in the 3x3 neighbourhood and add 1 to each unordered pair.
    Vectorised: gather the 8 neighbours per zero pixel into a (P, 8) array, sort
    each row, blank repeats, then emit the 28 column pairs. Because repeats are
    blanked, a row's surviving entries are exactly its distinct labels, so this
    reproduces MATLAB's ``nchoosek(unique(...))`` count including the original's
    triple/quadruple counting at three- and four-cell junctions.
    """
    rows, cols = [], []
    n_rows = mask.shape[0]
    offsets = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]
    for start in range(0, n_rows, block_rows):
        stop = min(start + block_rows, n_rows)
        lo, hi = max(start - 1, 0), min(stop + 1, n_rows)
        blk = mask[lo:hi]
        pad = np.pad(blk, 1, mode="constant", constant_values=0)
        core = pad[1 + (start - lo) : 1 + (stop - lo), 1:-1]
        zero_yx = np.nonzero(core == 0)
        if zero_yx[0].size == 0:
            continue
        # Coordinates back in `pad` space.
        py = zero_yx[0] + 1 + (start - lo)
        px = zero_yx[1] + 1
        nb = np.empty((py.size, 8), dtype=np.int64)
        for k, (dy, dx) in enumerate(offsets):
            nb[:, k] = pad[py + dy, px + dx]
        nb.sort(axis=1)
        dup = np.zeros_like(nb, dtype=bool)
        dup[:, 1:] = nb[:, 1:] == nb[:, :-1]
        nb[dup] = 0
        for i in range(8):
            ci = nb[:, i]
            for j in range(i + 1, 8):
                cj = nb[:, j]
                sel = (ci > 0) & (cj > 0)
                if sel.any():
                    rows.append(ci[sel])
                    cols.append(cj[sel])
    if not rows:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty
    return np.concatenate(rows), np.concatenate(cols)


def shared_boundary_matrix(
    mask: NDArray, block_rows: int = DEFAULT_BLOCK_ROWS
) -> Tuple[sp.csr_matrix, int]:
    """Row-normalised shared-perimeter matrix ``W[a, b] = shared(a,b)/perimeter(a)``.

    Both mask conventions are counted and the counts are added, so a mask that is
    partly walled and partly touching is handled without a mode switch. They
    cannot double-count the same interface: a direct contact contributes to the
    walled count only if a zero pixel sits within one pixel of it, which is by
    definition a *different* place along the interface.

    ``perimeter(a)`` is the row sum, i.e. cell-facing interface only —
    background-facing perimeter is excluded, matching
    ``cellBoundaryTotal = sum(cellPairMap, 1)`` in the original.

    Returns ``(W, n_neighbourless)``.

    The original divides by ``cellBoundaryTotal`` unguarded, so an isolated cell
    (no neighbour, row sum 0) yields 0/0 = NaN, and the NaN then propagates
    through the mat-mul into *every* cell's compensated value. Here such rows are
    left at zero, which makes an isolated cell's signal pass through unchanged —
    the physically right answer, since a cell with no neighbour receives no leak.
    """
    n = int(mask.max())
    dim = n + 1  # label-indexed; row/col 0 is background and stays empty

    r1, c1 = _neighbour_pairs_direct(mask, block_rows)
    r2, c2 = _neighbour_pairs_walled(mask, block_rows)
    rows = np.concatenate([r1, r2])
    cols = np.concatenate([c1, c2])

    if rows.size == 0:
        return sp.csr_matrix((dim, dim), dtype=np.float64), int((np.bincount(
            mask.ravel(), minlength=dim)[1:] > 0).sum())

    # Symmetrise by recording both directions, then sum duplicates.
    both_r = np.concatenate([rows, cols])
    both_c = np.concatenate([cols, rows])
    shared = sp.coo_matrix(
        (np.ones(both_r.size, dtype=np.float64), (both_r, both_c)), shape=(dim, dim)
    ).tocsr()
    shared.sum_duplicates()

    perimeter = np.asarray(shared.sum(axis=1)).ravel()
    has_neighbour = perimeter > 0
    inv = np.zeros_like(perimeter)
    inv[has_neighbour] = 1.0 / perimeter[has_neighbour]
    weights = sp.diags(inv) @ shared

    present = np.zeros(dim, dtype=bool)
    present[np.unique(mask)] = True
    present[0] = False
    n_neighbourless = int((present & ~has_neighbour).sum())
    return weights.tocsr(), n_neighbourless


def boundary_band(
    mask: NDArray,
    element_size: int,
    element_shape: str = "disk",
    block_rows: int = DEFAULT_BLOCK_ROWS,
) -> NDArray[np.bool_]:
    """Cell pixels within ``element_size`` of their own cell's edge.

    DELIBERATE DIFFERENCE FROM THE ORIGINAL. MATLAB dilates the *zero* pixels by
    a structuring element and intersects the result with each cell. That is only
    equivalent to "within element_size of this cell's own edge" when cells are
    separated by a zero wall, which modern segmenters do not produce; and the
    structuring elements it offers (``strel('square')`` = Chebyshev,
    ``strel('diamond')`` = taxicab) are noticeably anisotropic once the radius is
    more than a few pixels — a square element of radius 10 reaches 14.1 px along
    the diagonals and 10 px along the axes.

    Here the band is defined directly: mark each cell's own inner rim (the cell
    pixels adjacent to anything that is not that cell — background, wall or
    another cell), then take everything within ``element_size - 1`` of that rim,
    under the metric named by ``element_shape``. ``element_size=1`` is the bare
    rim; ``element_size=k`` is a k-pixel-deep rim. This is identical to the
    original at small radii on a walled mask, and is well-defined on a touching
    mask where the original is not.

    ``disk`` (Euclidean) is the default and the one to use at the radii a
    fluorescence WSI needs; ``square``/``diamond`` reproduce the original's two
    structuring elements for comparison.
    """
    if element_size < 1:
        raise ValueError(f"element_size must be >= 1, got {element_size}")
    try:
        metric = ELEMENT_SHAPES[element_shape]
    except KeyError:
        raise ValueError(
            f"element_shape must be one of {sorted(ELEMENT_SHAPES)}, got {element_shape!r}"
        ) from None

    out = np.zeros(mask.shape, dtype=bool)
    halo = element_size + 1
    n_rows = mask.shape[0]
    for start in range(0, n_rows, block_rows):
        stop = min(start + block_rows, n_rows)
        lo, hi = max(start - halo, 0), min(stop + halo, n_rows)
        blk = mask[lo:hi]
        rim = _inner_rim(blk)
        if metric == "euclidean":
            dist = ndimage.distance_transform_edt(~rim)
        else:
            dist = ndimage.distance_transform_cdt(~rim, metric=metric)
        band = (blk > 0) & (dist <= element_size - 1)
        out[start:stop] = band[start - lo : stop - lo]
    return out


def _inner_rim(mask: NDArray) -> NDArray[np.bool_]:
    """Cell pixels 8-adjacent to a pixel that is not the same cell.

    ``skimage.segmentation.find_boundaries(mask, mode='inner')`` computes the same
    thing; done here with two grey morphology passes so this module stays on the
    NumPy/SciPy stack that every ``bin/`` script already has.
    """
    lo = ndimage.grey_erosion(mask, size=3, mode="constant", cval=0)
    hi = ndimage.grey_dilation(mask, size=3, mode="constant", cval=0)
    return (mask > 0) & ((lo != mask) | (hi != mask))


def redsea_geometry(
    mask: NDArray,
    element_size: int,
    element_shape: str = "disk",
    block_rows: int = DEFAULT_BLOCK_ROWS,
) -> RedseaGeometry:
    """Everything REDSEA needs that does not depend on the channel.

    One pass per patient. The result is what gets handed to every one of that
    patient's per-marker ``QUANTIFY`` tasks.
    """
    mask = np.ascontiguousarray(np.asarray(mask).squeeze())
    if mask.ndim != 2:
        raise ValueError(f"mask must be 2D, got shape {mask.shape}")
    if not np.issubdtype(mask.dtype, np.integer):
        mask = mask.astype(np.int64)

    weights, n_neighbourless = shared_boundary_matrix(mask, block_rows)
    band = boundary_band(mask, element_size, element_shape, block_rows)

    dim = int(mask.max()) + 1
    flat = mask.ravel()
    cell_px = np.bincount(flat, minlength=dim)
    band_px = np.bincount(np.where(band.ravel(), flat, 0), minlength=dim)
    band_px[0] = 0

    labels = np.unique(flat)
    labels = labels[labels != 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.divide(band_px[labels], cell_px[labels], dtype=np.float64)
    frac = np.nan_to_num(frac, nan=0.0)

    return RedseaGeometry(
        weights=weights,
        band=band,
        labels=labels,
        band_fraction=frac,
        n_neighbourless=n_neighbourless,
    )


def compensate(
    raw_sum: NDArray,
    edge_sum: NDArray,
    weights: sp.csr_matrix,
    redsea_checker: int = 1,
) -> NDArray:
    """Apply the compensation to one channel's per-cell sums.

    ``raw_sum`` and ``edge_sum`` are label-indexed vectors of length ``n+1``
    (index 0 = background, ignored): the channel summed over each whole cell and
    over each cell's boundary band respectively.

    ``compensated = raw + R*edge - W.T @ edge``, clipped at 0 — the boundary-mode
    equation from ``MIBIboundary_compensation_boundarySA.m``, with ``R`` the
    ``REDSEAChecker`` reinforcement switch.
    """
    if redsea_checker not in (0, 1):
        raise ValueError(f"redsea_checker must be 0 or 1, got {redsea_checker}")
    raw_sum = np.asarray(raw_sum, dtype=np.float64)
    edge_sum = np.asarray(edge_sum, dtype=np.float64)
    if raw_sum.shape != edge_sum.shape:
        raise ValueError(
            f"raw/edge length mismatch: {raw_sum.shape} vs {edge_sum.shape}"
        )
    if weights.shape[0] != raw_sum.shape[0]:
        raise ValueError(
            f"weights is {weights.shape} but there are {raw_sum.shape[0]} labels"
        )
    leaked_in = weights.T @ edge_sum
    out = raw_sum + redsea_checker * edge_sum - leaked_in
    return np.clip(out, 0.0, None)


def recommend_element_size(
    cell_diameter_um: float, pixel_size_um: float, ref_fraction: float = 0.171
) -> int:
    """The paper's "proportional to cell size" rule, made explicit.

    The authors give two calibration points and no formula:

    ===============  ==============  ==============  ============
    modality         mean cell area  pixel size      elementSize
    ===============  ==============  ==============  ============
    MIBI-TOF         107 px          0.781 um/px     2
    CyCIF            325 px          (see below)     3-4
    ===============  ==============  ==============  ============

    MIBI's 512 px per 400 um field gives 0.781 um/px, so a 107 px cell is 65 um^2,
    an 11.7 px equivalent diameter, and elementSize 2 is 17.1% of that diameter.
    A 325 px cell is 20.3 px across and 3.5 px is 17.2% of it. The two points
    agree on the *ratio*, not on any absolute micron distance — which is the
    paper's statement that "the pixel number for expansion should be proportional
    to cell size", and is why porting the literal ``elementSize = 2`` to a
    finer-pixel modality under-covers the membrane badly.

    Geometrically the ratio is doing something simple: a band of depth
    ``0.171 * d`` inward from a disk of diameter ``d`` covers 1 - (1-2*0.171)^2 =
    56% of its area. Both published settings sit at that same 56%, so the rule is
    "the band is the outer ~56% of the cell by area".

    THE FORMULA OVER-SHOOTS ON REAL CELLS. It is disk geometry, and real
    segmented cells have more perimeter per unit area than a disk, so the same
    nominal depth covers more of them. Measured on a Voronoi-like mask of 58 px
    cells, this function's 11 px produces a 0.76 band fraction rather than 0.56.
    Treat the result as an upper bound and prefer :func:`calibrate_element_size`,
    which measures the fraction on the actual mask instead of assuming a shape.
    """
    if pixel_size_um <= 0:
        raise ValueError(f"pixel_size_um must be > 0, got {pixel_size_um}")
    if cell_diameter_um <= 0:
        raise ValueError(f"cell_diameter_um must be > 0, got {cell_diameter_um}")
    return max(1, int(round(ref_fraction * cell_diameter_um / pixel_size_um)))



def calibrate_element_size(
    mask: NDArray,
    target_fraction: float = 0.56,
    element_shape: str = "disk",
    max_size: int = 40,
    block_rows: int = DEFAULT_BLOCK_ROWS,
) -> Tuple[int, Dict[int, float]]:
    """Pick ``element_size`` from the data instead of from a formula.

    WHY THIS EXISTS. :func:`recommend_element_size` applies the paper's
    "proportional to cell size" rule, and that rule is derived on *disks*. Real
    segmented cells are not disks: an irregular or elongated cell has more
    perimeter per unit area, so the same nominal depth swallows more of it. On a
    Voronoi-like mask of 58 px cells the disk formula's 11 px measures out at a
    0.76 band fraction, not the 0.56 both published settings sit at -- the formula
    over-shoots by ~35% once cell shape is realistic.

    So: sweep the candidate depths, measure the band fraction each actually
    produces on *this* mask, and return the one closest to ``target_fraction``.
    The sweep is nearly free because the distance field is computed once and each
    candidate is a threshold on it, not a fresh morphological pass.

    ``target_fraction`` defaults to 0.56, which is where both of the paper's own
    calibration points land (MIBI elementSize 2, CyCIF 3-4).

    Returns ``(best_size, {size: measured_fraction})``.
    """
    if not 0.0 < target_fraction < 1.0:
        raise ValueError(f"target_fraction must be in (0,1), got {target_fraction}")
    try:
        metric = ELEMENT_SHAPES[element_shape]
    except KeyError:
        raise ValueError(
            f"element_shape must be one of {sorted(ELEMENT_SHAPES)}, got {element_shape!r}"
        ) from None

    mask = np.ascontiguousarray(np.asarray(mask).squeeze())
    dim = int(mask.max()) + 1
    sizes = list(range(1, max_size + 1))
    band_px = {s: np.zeros(dim, dtype=np.int64) for s in sizes}

    halo = max_size + 1
    n_rows = mask.shape[0]
    for start in range(0, n_rows, block_rows):
        stop = min(start + block_rows, n_rows)
        lo, hi = max(start - halo, 0), min(stop + halo, n_rows)
        blk = mask[lo:hi]
        rim = _inner_rim(blk)
        if metric == "euclidean":
            dist = ndimage.distance_transform_edt(~rim)
        else:
            dist = ndimage.distance_transform_cdt(~rim, metric=metric)
        core_mask = blk[start - lo : stop - lo]
        core_dist = dist[start - lo : stop - lo]
        flat_lbl = core_mask.ravel()
        flat_d = core_dist.ravel()
        for s in sizes:
            sel = flat_d <= s - 1
            band_px[s] += np.bincount(
                np.where(sel, flat_lbl, 0), minlength=dim
            )[:dim]

    cell_px = np.bincount(mask.ravel(), minlength=dim)[:dim]
    labels = np.unique(mask)
    labels = labels[labels != 0]
    table: Dict[int, float] = {}
    for s in sizes:
        counts = band_px[s][labels]
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.divide(counts, cell_px[labels], dtype=np.float64)
        table[s] = float(np.nan_to_num(frac, nan=0.0).mean()) if labels.size else 0.0

    best = min(sizes, key=lambda s: abs(table[s] - target_fraction))
    return best, table

def band_sums(
    mask: NDArray, band: NDArray, channel: NDArray, n_labels: Optional[int] = None
) -> Tuple[NDArray, NDArray]:
    """Label-indexed ``(whole-cell sum, boundary-band sum)`` for one channel.

    Two ``np.bincount`` passes over the flattened arrays — the same shape as the
    whole-cell sum ``quantify.py`` already computes, which is why turning REDSEA
    on costs roughly one extra pass per channel rather than a new stage.
    """
    mask = np.ascontiguousarray(np.asarray(mask).squeeze())
    channel = np.asarray(channel).squeeze()
    band = np.asarray(band).squeeze()
    if mask.shape != channel.shape:
        raise ValueError(f"mask/channel shape mismatch: {mask.shape} vs {channel.shape}")
    if mask.shape != band.shape:
        raise ValueError(f"mask/band shape mismatch: {mask.shape} vs {band.shape}")
    dim = (int(mask.max()) + 1) if n_labels is None else n_labels
    flat = mask.ravel()
    val = channel.ravel().astype(np.float64)
    whole = np.bincount(flat, weights=val, minlength=dim)[:dim]
    edge_labels = np.where(band.ravel(), flat, 0)
    edge = np.bincount(edge_labels, weights=val, minlength=dim)[:dim]
    whole[0] = 0.0
    edge[0] = 0.0
    return whole, edge
