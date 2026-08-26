"""Where the reference image's texture comes from.

TWO SOURCES, ONE SEAM, and the seam is what makes the paper reproducible.
Headline numbers use institutional DAPI crops, which a reviewer cannot
download. A committed SyntheticCropSource reproduces the same CONCLUSIONS at
the unit and mid rungs, so "every number reproducible from a committed
command" is satisfiable even though the headline pixels are not public.

Every source is deterministic under its seed; nothing here reads the clock.
"""

from __future__ import annotations

import abc

import numpy as np

__all__ = ["CropSource", "TiffCropSource", "SyntheticCropSource", "describe"]


class CropSource(abc.ABC):
    """A source of reference-frame DAPI texture."""

    @abc.abstractmethod
    def crop(self, size, seed):
        """Return an ``(H, W)`` float32 image in ``[0, 1]``."""

    @property
    @abc.abstractmethod
    def kind(self):
        """Short provenance label written into truth.json."""


class SyntheticCropSource(CropSource):
    """Generated nuclei: the releasable seed set.

    Nuclei are placed by rejection sampling at a realistic density and rendered
    as anisotropic Gaussian blobs. This is deliberately NOT the headline source
    -- reviewers discount synthetic texture -- but it is the one that ships.
    """

    kind = "synthetic"

    def __init__(self, nuclei_per_mm2=2500.0, pixel_size_um=0.325):
        self.nuclei_per_mm2 = float(nuclei_per_mm2)
        self.pixel_size_um = float(pixel_size_um)

    def crop(self, size, seed):
        h, w = int(size[0]), int(size[1])
        rng = np.random.default_rng(seed)
        area_mm2 = (h * self.pixel_size_um / 1000.0) * (w * self.pixel_size_um / 1000.0)
        n = max(1, int(self.nuclei_per_mm2 * area_mm2))

        img = np.zeros((h, w), dtype=np.float32)
        cx = rng.uniform(0, w, n)
        cy = rng.uniform(0, h, n)
        radius = rng.normal(6.0 / self.pixel_size_um * 0.325, 1.2, n).clip(2.0, 20.0)
        bright = rng.uniform(0.4, 1.0, n)

        ys, xs = np.mgrid[0:h, 0:w]
        for x0, y0, r, b in zip(cx, cy, radius, bright):
            lo_y, hi_y = max(0, int(y0 - 3 * r)), min(h, int(y0 + 3 * r) + 1)
            lo_x, hi_x = max(0, int(x0 - 3 * r)), min(w, int(x0 + 3 * r) + 1)
            if lo_y >= hi_y or lo_x >= hi_x:
                continue
            dy = ys[lo_y:hi_y, lo_x:hi_x] - y0
            dx = xs[lo_y:hi_y, lo_x:hi_x] - x0
            img[lo_y:hi_y, lo_x:hi_x] += b * np.exp(-(dx**2 + dy**2) / (2 * r**2))

        peak = img.max()
        if peak > 0:
            img /= peak
        return img.astype(np.float32)


class TiffCropSource(CropSource):
    """Real institutional DAPI crops -- the headline source.

    Crops are drawn deterministically from the given slides; the seed selects
    both the slide and the window, so a (paths, seed) pair always yields the
    same pixels.
    """

    kind = "institutional"

    def __init__(self, paths, channel=0):
        if not paths:
            raise ValueError("TiffCropSource needs at least one slide path")
        self.paths = [str(p) for p in paths]
        self.channel = int(channel)

    def crop(self, size, seed):
        import tifffile  # lazy: the synthetic path must not require tifffile

        h, w = int(size[0]), int(size[1])
        rng = np.random.default_rng(seed)
        path = self.paths[int(rng.integers(len(self.paths)))]
        with tifffile.TiffFile(path) as tf:
            arr = tf.series[0].asarray()
        if arr.ndim == 3:
            arr = arr[self.channel]
        ah, aw = arr.shape[:2]
        if ah < h or aw < w:
            raise ValueError(f"{path} is {ah}x{aw}, smaller than the requested {h}x{w}")
        y0 = int(rng.integers(0, ah - h + 1))
        x0 = int(rng.integers(0, aw - w + 1))
        out = arr[y0:y0 + h, x0:x0 + w].astype(np.float32)
        peak = out.max()
        return (out / peak if peak > 0 else out).astype(np.float32)


def describe(source):
    """Provenance dict for truth.json."""
    meta = {"kind": source.kind, "releasable": source.kind == "synthetic"}
    if isinstance(source, TiffCropSource):
        meta["n_slides"] = len(source.paths)
        meta["channel"] = source.channel
    return meta
