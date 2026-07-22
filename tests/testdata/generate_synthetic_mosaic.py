"""Synthetic stitched mosaic with a KNOWN grid, vignette, darkfield, background.

Used as ground truth for illumination-correction tests: every recovered
quantity can be compared against the value injected here.
"""
from __future__ import annotations
import numpy as np


def _vignette(tile: int, strength: float) -> np.ndarray:
    """Radial mean-1 flat-field: bright center, dark corners."""
    yy, xx = np.mgrid[0:tile, 0:tile].astype(np.float64)
    cy = cx = (tile - 1) / 2.0
    r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    r /= r.max()
    ff = 1.0 - strength * (r ** 2)      # smooth quadratic falloff
    ff /= ff.mean()
    return ff.astype(np.float32)


def _structured_content(rng, tile, n_cells=12):
    """Dim SMOOTH autofluorescence baseline + sparse bright Gaussian 'cells'.

    Closer to real IF than spatially-white noise: the low-intensity pixels form a
    genuine SMOOTH background (a near-constant dim level + small noise), so the
    dominant background variation across the mosaic is the vignette itself — which
    is exactly what flat-field correction targets and a flatness metric should see
    improve. Signal is sparse and bright (so signal-fidelity is measurable). The
    clean (pre-optics) content is stored for full-reference validation.
    """
    content = 30.0 + rng.normal(0, 2.0, (tile, tile))       # smooth dim background
    yy, xx = np.mgrid[0:tile, 0:tile].astype(np.float64)
    for _ in range(n_cells):
        cy, cx = rng.uniform(0, tile), rng.uniform(0, tile)
        r = rng.uniform(2.5, 5.0)
        amp = rng.uniform(1500, 4000)
        content += amp * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * r * r))
    return content


def make_synthetic_mosaic(n_tiles_y=4, n_tiles_x=5, tile=128, overlap=16,
                          n_channels=2, seed=0, vignette_strength=0.45,
                          dark=200.0, bg_amp=0.0, structured=False):
    rng = np.random.default_rng(seed)
    pitch = tile - overlap
    Y = n_tiles_y * pitch + overlap
    X = n_tiles_x * pitch + overlap
    ff = _vignette(tile, vignette_strength)

    mosaic = np.zeros((n_channels, Y, X), dtype=np.float64)
    clean = np.zeros((n_channels, Y, X), dtype=np.float64)   # ground-truth content
    weight = np.zeros((Y, X), dtype=np.float64)

    for ty in range(n_tiles_y):
        for tx in range(n_tiles_x):
            y0, x0 = ty * pitch, tx * pitch
            for c in range(n_channels):
                # 'tissue' content per tile, same optics (ff) every tile
                if structured:
                    content = _structured_content(rng, tile)
                else:
                    content = rng.uniform(500, 4000) * rng.random((tile, tile))
                signal = content * ff + dark
                mosaic[c, y0:y0 + tile, x0:x0 + tile] += signal
                clean[c, y0:y0 + tile, x0:x0 + tile] += content
            weight[y0:y0 + tile, x0:x0 + tile] += 1.0

    # blend overlaps by averaging (the stitcher's feather, simplified)
    mosaic /= np.maximum(weight, 1.0)[None]
    clean /= np.maximum(weight, 1.0)[None]

    if bg_amp > 0:
        yy, xx = np.mgrid[0:Y, 0:X].astype(np.float64)
        bg = bg_amp * (0.5 + 0.5 * np.sin(2 * np.pi * yy / Y) * np.cos(2 * np.pi * xx / X))
        mosaic += bg[None]

    mosaic = np.clip(mosaic, 0, 65535).astype(np.uint16)
    names = [f"CH{i}" for i in range(n_channels)]
    return {
        "mosaic": mosaic,
        "clean": clean,              # ground-truth content (pre-optics), for full-reference metrics
        "flatfield": ff,
        "dark": float(dark),
        "pitch": float(pitch),
        "phase": 0,
        "channel_names": names,
    }
