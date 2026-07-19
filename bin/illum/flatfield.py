"""Per-channel periodic flat-field: estimate at reduced resolution, apply chunked."""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter, zoom
from illum.grid import tile_origins


def estimate_flatfield(channel, grid, smooth_frac=0.12, est_downsample=4):
    py, px = int(round(grid["pitch_y"])), int(round(grid["pitch_x"]))
    Y, X = channel.shape
    ys = tile_origins(grid["phase_y"], grid["pitch_y"], py, Y)
    xs = tile_origins(grid["phase_x"], grid["pitch_x"], px, X)

    # Per-tile content is spatially white (independent per-pixel brightness),
    # so a single point sample per est_downsample block (strided subsampling)
    # leaves substantial noise in the raw per-tile estimate. Instead, each
    # tile is block-average (area-mean) downsampled to the estimate grid
    # BEFORE being stacked, so only reduced-resolution tiles are ever held
    # in memory together (~16x less than stacking full-res tiles). The
    # median across those reduced tiles (robust to tile-to-tile
    # content-brightness skew) then isolates the smooth vignette from
    # per-tile content noise just as well as combining at full res would.
    ds = max(int(est_downsample), 1)
    small_h, small_w = max(py // ds, 1), max(px // ds, 1)
    th, tw = small_h * ds, small_w * ds

    tiles = []
    for y in ys:
        for x in xs:
            t = channel[y:y + py, x:x + px].astype(np.float32)
            m = np.median(t)
            if m > 0:
                t_small = t[:th, :tw].reshape(small_h, ds, small_w, ds).mean(axis=(1, 3))
                tiles.append(t_small / m)
    if not tiles:
        raise RuntimeError("no complete tiles; check pitch/phase/approx_tile")

    ff_small = np.median(np.stack(tiles, 0), axis=0)

    # Each tile already went through a block-average before landing in the
    # stack, so the median above is combining pre-denoised samples rather
    # than raw per-pixel noise. Reusing the full smooth_frac-derived sigma
    # (tuned for smoothing a noisier, un-block-averaged median) over-blurs
    # the small ff and washes out real vignette curvature instead of just
    # denoising it, which weakens the correction. Halving the sigma keeps
    # comparable noise suppression for the (already-denoised) input while
    # preserving vignette shape.
    ff_small = gaussian_filter(ff_small, sigma=max(small_h, small_w) * smooth_frac * 0.5)
    ff = zoom(ff_small, (py / ff_small.shape[0], px / ff_small.shape[1]), order=1)
    ff = ff[:py, :px].astype(np.float32)
    ff /= ff.mean()
    return ff


def _field_index(coords, phase, pitch, ff_len):
    frac = ((coords - phase) % pitch) / pitch      # [0, 1)
    idx = (frac * ff_len).astype(np.int64)
    return np.clip(idx, 0, ff_len - 1)


def apply_field(channel, ff, grid, chunk_rows=2048, out_dtype=np.uint16):
    Y, X = channel.shape
    py, px = ff.shape
    ci = _field_index(np.arange(X), grid["phase_x"], grid["pitch_x"], px)
    info = np.iinfo(out_dtype)
    out = np.empty((Y, X), dtype=out_dtype)
    for y0 in range(0, Y, chunk_rows):
        y1 = min(y0 + chunk_rows, Y)
        ri = _field_index(np.arange(y0, y1), grid["phase_y"], grid["pitch_y"], py)
        field = ff[np.ix_(ri, ci)]
        corr = channel[y0:y1].astype(np.float32) / field
        out[y0:y1] = np.clip(corr, info.min, info.max).astype(out_dtype)
    return out
