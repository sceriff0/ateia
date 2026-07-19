"""QC metrics (seam suppression, background flatness) + resource capture."""
from __future__ import annotations
import sys, time, resource
import numpy as np
from scipy.ndimage import gaussian_filter1d


def _detrend(x, frac=0.05):
    x = x.astype(np.float64)
    sig = max(len(x) * frac, 3.0)
    return x - gaussian_filter1d(x, sig)


def seam_peak(channel, grid, axis) -> float:
    prof = np.median(channel, axis=1 - axis).astype(np.float64)
    period = grid["pitch_x"] if axis == 1 else grid["pitch_y"]
    x = _detrend(prof)
    x = (x - x.mean()) * np.hanning(len(x))
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(prof))
    kf = int(np.argmin(np.abs(freqs - 1.0 / period)))
    band = power[max(kf - 2, 0):kf + 3].sum()
    return float(band / (power.sum() + 1e-12))


def background_cv(channel, pct=40.0) -> float:
    thr = np.percentile(channel, pct)
    bg = channel[channel <= thr].astype(np.float64)
    return float(bg.std() / (bg.mean() + 1e-12))


def _rss_mb():
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def measure(fn, *args, **kw):
    t0 = time.perf_counter()
    r0 = _rss_mb()
    result = fn(*args, **kw)
    stats = {"seconds": time.perf_counter() - t0,
             "peak_rss_mb": max(_rss_mb() - r0, 0.0)}
    return result, stats
