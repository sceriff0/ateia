"""Generate a (size x channels) benchmark matrix from a single source image.

Pure functions (compute_target_shape, synthesize_channels) are unit-tested.
Heavy I/O (read/resize/write OME-TIFF) is isolated in run_matrix().
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np


def compute_target_shape(src_hw: tuple[int, int], target_long_edge: int) -> tuple[int, int]:
    """Scale (height, width) so the longer edge equals target_long_edge, preserving aspect."""
    h, w = src_hw
    long_edge = max(h, w)
    scale = target_long_edge / float(long_edge)
    return (round(h * scale), round(w * scale))


def synthesize_channels(src_2d: np.ndarray, n_channels: int, seed: int = 0) -> np.ndarray:
    """Replicate a single 2-D channel into n_channels with per-channel perturbation.

    Channel 0 is the unmodified source. Channels 1..N-1 add intensity jitter,
    Gaussian noise, and a 1-px roll offset so each channel is non-identical.
    """
    if src_2d.ndim != 2:
        raise ValueError("src_2d must be 2-D (H, W)")
    rng = np.random.default_rng(seed)
    info = np.iinfo(src_2d.dtype)
    out = np.empty((n_channels,) + src_2d.shape, dtype=src_2d.dtype)
    out[0] = src_2d
    for c in range(1, n_channels):
        gain = 1.0 + rng.uniform(-0.1, 0.1)
        noise = rng.normal(0.0, 3.0, size=src_2d.shape)
        shifted = np.roll(src_2d, shift=c, axis=1)
        vals = np.clip(shifted.astype(np.float64) * gain + noise, info.min, info.max)
        out[c] = vals.astype(src_2d.dtype)
    return out
