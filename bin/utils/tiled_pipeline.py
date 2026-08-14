"""The STARE registration method: one module, two drivers.

STARE is five steps — global rigid anchor, per-tile residual, TRE-gated control grid, mesh,
warp — and it ships in two shapes. The production shape is a four-process Nextflow fan-out
(``bin/tiled_coarse.py`` -> ``tiled_reg_tile.py`` (one task per tile) -> ``tiled_solve.py`` ->
``tiled_stitch.py``). The other is :func:`register_slide`, the same five steps in one call, used
as the in-process oracle by the accuracy benchmark and the unit tests.

They used to be two independent orchestrations over shared primitives, and they DIFFERED in two
ways that no test could see. Both differences now live here as explicit options, so a driver
selects behaviour instead of reimplementing it:

``coarse_anchor(..., factor)``
    The fan-out estimates the anchor on a **decimated thumbnail** and lifts the fit back to
    full-resolution pixels (``factor > 1``); the in-process driver estimates at native
    resolution (``factor == 1``, where the lift is the identity). Forgetting the lift
    under-translates the whole slide by exactly the factor, with a clean exit code — which is
    why it is one function with a factor argument rather than two code paths.

:class:`TileWindows`
    ``lazy=True`` (production) reads only the tile's reference window and the small moving crop
    the tile's inverse map draws from — peak memory is one tile whatever the slide size.
    ``lazy=False`` rigid-pre-warps the whole moving plane once and slices tiles out of it —
    fewer moving parts, peak memory the whole slide. Warping a window equals cropping the
    full-frame warp (see :func:`tiled_warp.warp_image`), which is why they agree.

What stays outside this module is genuinely per-driver: the fan-out's lazy banded reads and
per-task logging, and the reduction over serialized per-tile JSON. The gate itself is
:func:`tiled_manifest.assemble_control_grid`, called by both.

Registration is estimated from the nuclear/fiducial channel; the same transform warps any
channel. Pure NumPy + scikit-image — no VALIS, no JVM.
"""

from __future__ import annotations

import numpy as np
from coarse_align import estimate_rigid, scale_transform_to_full_res
from mesh_field import MeshField
from tile_grid import tile_grid
from tile_residual import residual_displacement
from tiled_io import decimation_factor
from tiled_manifest import assemble_control_grid, slide_entry
from tiled_warp import source_region, warp_image

__all__ = ["ArraySource", "TileWindows", "coarse_anchor", "register_slide"]


def coarse_anchor(ref_plane, mov_plane, factor=1, *, model="euclidean"):
    """Global rigid ``M0`` in FULL-RESOLUTION pixels, from planes decimated by ``factor``.

    A decimated coordinate relates to the full-resolution one by ``p_ds = p_full / factor``, so
    the fit and its residual must both be lifted before anything downstream — the tile plan,
    each tile's ``source_region``, the stitch — uses them. ``factor == 1`` makes the lift an
    identity, which is why the native-resolution driver goes through this function too rather
    than round its own corner.

    Returns ``(M0, coarse_tre_px, n_inliers)``, the TRE in full-resolution pixels.
    """
    m0_ds, tre_ds, n_inliers = estimate_rigid(ref_plane, mov_plane, model=model)
    return (
        scale_transform_to_full_res(m0_ds, factor),
        float(tre_ds) * factor,
        int(n_inliers),
    )


class ArraySource:
    """An in-memory plane or ``(C, H, W)`` stack behind :func:`tiled_io.open_lazy`'s interface.

    Lets the in-process driver feed :class:`TileWindows` the same way the fan-out feeds it from
    a zarr view, so both drivers can select either windowing mode. A 2-D array is presented as
    ``C = 1``, matching ``open_lazy``.
    """

    def __init__(self, arr):
        a = np.asarray(arr)
        if a.ndim not in (2, 3):
            raise ValueError(f"expected 2-D or 3-D (C, H, W), got shape {a.shape}")
        self._a = a[np.newaxis, ...] if a.ndim == 2 else a
        self.shape = self._a.shape
        self.dtype = self._a.dtype

    def __getitem__(self, key):
        c, ys, xs = key
        return self._a[c, ys, xs]


class TileWindows:
    """Produces the ``(reference tile, rigid-warped moving tile)`` pair a residual is measured on.

    Construct once per slide, call once per tile read box ``(rx0, ry0, rx1, ry1)`` in the
    reference frame. See the module docstring for what ``lazy`` costs and buys; the lazy mode
    is the one that ships, and it is what TILED_REG_TILE runs once per tile.
    """

    def __init__(self, ref_src, mov_src, m0, *, nuclear_index=0, lazy=True):
        self._ref = ref_src
        self._mov = mov_src
        self._m0 = np.asarray(m0, dtype=float)
        self._index = int(nuclear_index)
        self._lazy = bool(lazy)
        self._eager_planes = None  # materialised on first use, never in lazy mode

    def _planes(self):
        """The full reference plane and the whole-slide rigid pre-warp of the moving plane."""
        if self._eager_planes is None:
            _c, h, w = self._ref.shape
            ref_plane = np.asarray(
                self._ref[self._index, slice(0, h), slice(0, w)], dtype=float
            )
            _mc, mh, mw = self._mov.shape
            mov_plane = np.asarray(
                self._mov[self._index, slice(0, mh), slice(0, mw)], dtype=float
            )
            self._eager_planes = (
                ref_plane,
                warp_image(mov_plane, self._m0, None, ref_plane.shape),
            )
        return self._eager_planes

    def __call__(self, read_box):
        rx0, ry0, rx1, ry1 = (int(v) for v in read_box)
        if not self._lazy:
            ref_plane, mov_rigid = self._planes()
            return ref_plane[ry0:ry1, rx0:rx1], mov_rigid[ry0:ry1, rx0:rx1]

        out_h, out_w = ry1 - ry0, rx1 - rx0
        ref_tile = np.asarray(
            self._ref[self._index, slice(ry0, ry1), slice(rx0, rx1)], dtype=np.float32
        )
        _c, mh, mw = self._mov.shape
        sx0, sy0, sx1, sy1 = source_region(
            self._m0, None, (rx0, ry0), (out_h, out_w), src_shape=(mh, mw)
        )
        if sx1 > sx0 and sy1 > sy0:
            crop = np.asarray(
                self._mov[self._index, slice(sy0, sy1), slice(sx0, sx1)], dtype=float
            )
            mov_tile = warp_image(
                crop,
                self._m0,
                None,
                (out_h, out_w),
                out_origin=(rx0, ry0),
                src_origin=(sx0, sy0),
            )
        else:
            # the tile's source box falls entirely outside the moving slide
            mov_tile = np.zeros((out_h, out_w), dtype=float)
        return ref_tile, mov_tile


def register_slide(
    ref_dapi,
    mov_dapi,
    *,
    tile=512,
    halo=64,
    gate_tre=1.0,
    upsample=10,
    model="euclidean",
    warp_data=None,
    out_shape=None,
    coarse_max_dim=0,
    lazy_tiles=False,
):
    """Register ``mov_dapi`` to ``ref_dapi`` and warp an image into the reference frame.

    Parameters
    ----------
    ref_dapi, mov_dapi : 2-D arrays
        Reference and moving DAPI images used to *estimate* the transform.
    tile, halo : int
        Tile core size and read halo (px). Tile size is the mesh grid resolution.
    gate_tre : float
        Only tiles whose rigid-stage residual TRE (px) meets this threshold contribute a mesh
        displacement; the rest stay rigid.
    warp_data : ndarray, optional
        Image actually warped to produce the registered output (e.g. the full multi-channel
        moving slide). Defaults to ``mov_dapi``.
    out_shape : (int, int), optional
        Output raster ``(H, W)``. Defaults to the reference DAPI shape.
    coarse_max_dim : int
        Longest thumbnail side (px) the anchor is estimated on; ``<= 0`` (the default here)
        estimates at native resolution. The production driver sets this from
        ``params.reg_tiled_coarse_max_dim``, because ORB costs roughly the SQUARE of the pixel
        count — see ``bin/tiled_coarse.py``. Whatever the value, the anchor comes back in
        full-resolution pixels.
    lazy_tiles : bool
        ``False`` (the default here) rigid-pre-warps the whole moving plane once and slices
        tiles out of it. ``True`` selects the production windowing: only each tile's own
        reference window and moving crop are read. Same maths, different peak memory — see
        :class:`TileWindows`.

    Returns a dict with ``registered``, ``entry`` (manifest slide entry), ``mesh``, ``M0``,
    ``tiles``, ``residuals``, ``tre_px`` (per-tile TRE), ``coarse_tre`` and ``n_inliers``.
    """
    ref_dapi = np.asarray(ref_dapi, dtype=float)
    mov_dapi = np.asarray(mov_dapi, dtype=float)
    out_shape = tuple(out_shape) if out_shape is not None else ref_dapi.shape

    # 1. global rigid anchor, through the same decimate-and-lift contract the fan-out uses.
    #    coarse_max_dim <= 0 -> factor 1 -> the strides and the lift are both identities.
    factor = decimation_factor([ref_dapi.shape, mov_dapi.shape], coarse_max_dim)
    m0, coarse_tre, n_inliers = coarse_anchor(
        ref_dapi[::factor, ::factor],
        mov_dapi[::factor, ::factor],
        factor,
        model=model,
    )

    # 2. measure the residual per reference tile, through the same windowing the fan-out uses
    windows = TileWindows(
        ArraySource(ref_dapi), ArraySource(mov_dapi), m0, lazy=lazy_tiles
    )
    tiles = tile_grid(ref_dapi.shape[1], ref_dapi.shape[0], tile, halo)
    residuals = []
    for t in tiles:
        ref_tile, mov_tile = windows(t.read)
        dx, dy, tre = residual_displacement(ref_tile, mov_tile, upsample=upsample)
        residuals.append((dx, dy, tre))

    # 3. TRE-gated control grid (reference-frame tile centres) -> manifest entry + mesh
    grid_x, grid_y, disp = assemble_control_grid(tiles, residuals, gate_tre)
    entry = slide_entry(m0, grid_x, grid_y, disp)
    mesh = (
        MeshField(grid_x, grid_y, np.asarray(disp, dtype=float))
        if entry["mesh"] is not None
        else None
    )

    # 4. Post-refinement per-tile residual = STARE's final-accuracy TRE (the analogue of VALIS's
    #    non-rigid error). Re-warp the DAPI with M0 + mesh and re-measure each tile against the
    #    reference. With no mesh the rigid warp is final, so the rigid residual is already the
    #    final residual — no second measurement needed.
    if mesh is not None:
        mov_refined = warp_image(mov_dapi, m0, mesh, ref_dapi.shape)
        tre_after = []
        for t in tiles:
            rx0, ry0, rx1, ry1 = t.read
            _dx, _dy, tre = residual_displacement(
                ref_dapi[ry0:ry1, rx0:rx1],
                mov_refined[ry0:ry1, rx0:rx1],
                upsample=upsample,
            )
            tre_after.append(tre)
    else:
        tre_after = [r[2] for r in residuals]

    # 5. warp the (possibly multi-channel) moving image with M0 + mesh
    data = warp_data if warp_data is not None else mov_dapi
    registered = warp_image(data, m0, mesh, out_shape)

    return {
        "registered": registered,
        "entry": entry,
        "mesh": mesh,
        "M0": m0,
        "tiles": tiles,
        "residuals": residuals,
        "tre_px": [r[2] for r in residuals],  # per-tile rigid-stage misalignment
        "tre_after_px": tre_after,  # per-tile residual after refinement (final accuracy)
        "coarse_tre": coarse_tre,
        "n_inliers": n_inliers,
    }
