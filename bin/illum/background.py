"""Diffuse-background removal methods (non-periodic additive signal).

Applied AFTER flat-field/darkfield. Each returns clipped out_dtype. skimage-only
methods degrade gracefully when skimage is absent.
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter, grey_opening, median_filter, zoom

try:
    from skimage.restoration import rolling_ball as _rolling_ball
    _HAS_SKIMAGE = True
except Exception:
    _HAS_SKIMAGE = False


def _finish(channel, corr, out_dtype):
    info = np.iinfo(out_dtype)
    return np.clip(corr, info.min, info.max).astype(out_dtype)


def _none(channel, out_dtype=np.uint16, **kw):
    return channel.astype(out_dtype, copy=True)


def _opening(channel, out_dtype=np.uint16, downsample=8, radius=None, **kw):
    f = channel.astype(np.float32)
    small = f[::downsample, ::downsample]
    r = max((radius // downsample), 1) if radius else max(min(small.shape) // 12, 1)
    bg_small = grey_opening(small, size=(2 * r + 1, 2 * r + 1))
    bg_small = gaussian_filter(bg_small, r / 2)
    bg = zoom(bg_small, (f.shape[0] / bg_small.shape[0], f.shape[1] / bg_small.shape[1]), order=1)
    return _finish(channel, f - bg[:f.shape[0], :f.shape[1]], out_dtype)


def _gaussian(channel, out_dtype=np.uint16, sigma=50, **kw):
    f = channel.astype(np.float32)
    bg = gaussian_filter(f, sigma=sigma)
    return _finish(channel, f - bg, out_dtype)


def _median(channel, out_dtype=np.uint16, downsample=8, size=15, **kw):
    f = channel.astype(np.float32)
    small = f[::downsample, ::downsample]
    bg_small = median_filter(small, size=size)
    bg = zoom(bg_small, (f.shape[0] / bg_small.shape[0], f.shape[1] / bg_small.shape[1]), order=1)
    return _finish(channel, f - bg[:f.shape[0], :f.shape[1]], out_dtype)


def _tophat(channel, out_dtype=np.uint16, downsample=8, radius=None, **kw):
    # white top-hat == img - opening(img); reuse the opening background
    return _opening(channel, out_dtype=out_dtype, downsample=downsample, radius=radius)


def _rball(channel, out_dtype=np.uint16, radius=50, **kw):
    f = channel.astype(np.float32)
    bg = _rolling_ball(f, radius=radius)
    return _finish(channel, f - bg, out_dtype)


BACKGROUND_METHODS = {
    "none": _none,
    "opening": _opening,
    "gaussian": _gaussian,
    "median": _median,
    "tophat": _tophat,
}
if _HAS_SKIMAGE:
    BACKGROUND_METHODS["rolling_ball"] = _rball


def available_methods():
    return list(BACKGROUND_METHODS.keys())


def remove_background(channel, method, out_dtype=np.uint16, **kw):
    if method not in BACKGROUND_METHODS:
        raise ValueError(f"unknown background method '{method}'; "
                         f"available: {available_methods()}")
    return BACKGROUND_METHODS[method](channel, out_dtype=out_dtype, **kw)
