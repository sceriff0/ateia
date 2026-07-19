"""Additive darkfield: constant camera pedestal or spatially-varying floor."""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter, zoom
from illum.grid import tile_origins
from illum.flatfield import _field_index


def estimate_dark_constant(channel, pct=1.0) -> float:
    return float(np.percentile(channel, pct))


def estimate_dark_field(channel, grid, pct=5.0, smooth_frac=0.2, est_downsample=4):
    py, px = int(round(grid["pitch_y"])), int(round(grid["pitch_x"]))
    Y, X = channel.shape
    ys = tile_origins(grid["phase_y"], grid["pitch_y"], py, Y)
    xs = tile_origins(grid["phase_x"], grid["pitch_x"], px, X)
    ds = max(int(est_downsample), 1)
    small_h, small_w = max(py // ds, 1), max(px // ds, 1)
    tiles = []
    for y in ys:
        for x in xs:
            t = channel[y:y + py, x:x + px].astype(np.float32)
            tiles.append(t[::ds, ::ds][:small_h, :small_w])
    stack = np.stack(tiles, 0)
    dark_small = np.percentile(stack, pct, axis=0)
    dark_small = gaussian_filter(dark_small, sigma=max(small_h, small_w) * smooth_frac)
    dark = zoom(dark_small, (py / dark_small.shape[0], px / dark_small.shape[1]), order=1)
    return dark[:py, :px].astype(np.float32)


def subtract_dark(channel, dark, grid=None, chunk_rows=2048):
    f = channel.astype(np.float32)
    if np.isscalar(dark):
        return f - float(dark)
    # tile-sized darkfield: tile it back on the float grid
    Y, X = channel.shape
    py, px = dark.shape
    ci = _field_index(np.arange(X), grid["phase_x"], grid["pitch_x"], px)
    out = np.empty((Y, X), dtype=np.float32)
    for y0 in range(0, Y, chunk_rows):
        y1 = min(y0 + chunk_rows, Y)
        ri = _field_index(np.arange(y0, y1), grid["phase_y"], grid["pitch_y"], py)
        out[y0:y1] = f[y0:y1] - dark[np.ix_(ri, ci)]
    return out
