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
from pathlib import Path

import numpy as np

__all__ = ["CropSource", "TiffCropSource", "SyntheticCropSource", "describe",
           "from_config"]


class CropSource(abc.ABC):
    """A source of reference-frame DAPI texture."""

    @abc.abstractmethod
    def crop(self, size, seed):
        """Return an ``(H, W)`` float32 image in ``[0, 1]``."""

    @property
    @abc.abstractmethod
    def kind(self):
        """Short provenance label written into truth.json."""

    def validate(self, size):
        """Raise if any crop this source can produce at ``size`` would fail.

        Called ONCE, before the first pair is generated, so a source that
        cannot serve the committed ``size`` fails in seconds rather than
        partway through a multi-day sweep. This matters more than it looks:
        ``TiffCropSource`` picks its slide FROM THE SEED, so one undersized or
        missing slide in a list of five does not fail every pair -- it fails
        roughly one in five, scattered through the plan, hundreds of rows
        after the run started. Cheap upfront, unrecoverable late.

        The default is a no-op: a generated source has nothing to check.
        """
        return None


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

        # Existence is checked HERE, not at first crop, for the reason
        # `validate` documents: the seed selects the slide, so a missing path
        # fails a seed-dependent SUBSET of the plan rather than the whole run.
        missing = [q for q in self.paths if not Path(q).is_file()]
        if missing:
            raise FileNotFoundError(
                "TiffCropSource: slide path(s) do not exist: "
                + ", ".join(missing)
            )

    def validate(self, size):
        """Confirm every slide can serve a ``size`` crop, reading only headers.

        ``tifffile``'s series shape comes from the IFDs, so this never decodes
        pixels -- checking five gigapixel slides costs milliseconds, while
        ``crop`` itself calls ``asarray()`` and materialises a whole slide.
        """
        import tifffile

        h, w = int(size[0]), int(size[1])
        problems = []
        for q in self.paths:
            with tifffile.TiffFile(q) as tf:
                shape = tf.series[0].shape
            spatial = shape[-2:]
            if len(shape) >= 3 and self.channel >= shape[0]:
                problems.append(
                    f"{q}: channel {self.channel} out of range for shape {shape}"
                )
            elif spatial[0] < h or spatial[1] < w:
                problems.append(
                    f"{q}: {spatial[0]}x{spatial[1]} is smaller than the "
                    f"requested {h}x{w}"
                )
        if problems:
            raise ValueError(
                "TiffCropSource cannot serve the committed size:\n  "
                + "\n  ".join(problems)
            )

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
    """Provenance dict for truth.json.

    For a ``TiffCropSource`` this includes the actual slide ``paths``, not
    just their count: ``generate.param_hash`` hashes this dict, and two
    different institutional crop sets with the same slide COUNT and channel
    would otherwise hash identically, silently claiming to be the same
    generator input.
    """
    meta = {"kind": source.kind, "releasable": source.kind == "synthetic"}
    if isinstance(source, TiffCropSource):
        meta["n_slides"] = len(source.paths)
        meta["channel"] = source.channel
        meta["paths"] = list(source.paths)
    return meta


def from_config(config):
    """Build the ``CropSource`` the committed config names.

    THE ONE RULE HERE: an ``institutional`` request that cannot be satisfied
    RAISES. It never degrades to ``SyntheticCropSource``. A silent fallback
    would publish headline numbers measured on generated nuclei under a label
    claiming real tissue -- the exact "runs, passes, reports a plausible value
    that is not true" shape FROZEN.md's closing rule is about. The fallback
    direction that IS safe is the absent block: no ``crop_source:`` at all
    means synthetic, so the unit rung and CI stay hermetic without naming it.

    ``paths`` entries may be glob patterns. They are expanded and sorted, and
    the RESOLVED list is what ``TiffCropSource`` stores -- so ``describe()``
    writes the actual slides into ``truth.json`` and ``generate.param_hash``
    hashes them. Adding a slide to a globbed directory therefore changes the
    hash rather than silently changing the experiment. A pattern matching
    nothing raises: an empty expansion is how a typo'd cluster path turns into
    "no slides", and ``TiffCropSource`` would report that as the wrong error.
    """
    block = (config or {}).get("crop_source") or {}
    kind = str(block.get("kind", "synthetic")).strip().lower()

    if kind == "synthetic":
        kwargs = {k: float(block[k]) for k in ("nuclei_per_mm2", "pixel_size_um")
                  if k in block}
        return SyntheticCropSource(**kwargs)

    if kind == "institutional":
        patterns = block.get("paths") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not patterns:
            raise ValueError(
                "crop_source.kind is 'institutional' but no paths are listed. "
                "Refusing to fall back to synthetic texture: the headline "
                "result would then be measured on generated nuclei while "
                "claiming to be institutional."
            )

        # glob.glob, not Path.glob: cluster slide paths are absolute
        # (/hpcnfs/...), and Path().glob() rejects an absolute pattern outright
        # rather than expanding it.
        import glob as _glob

        resolved, empty = [], []
        for pattern in patterns:
            pattern = str(pattern)
            if any(ch in pattern for ch in "*?["):
                hits = sorted(_glob.glob(pattern))
                if not hits:
                    empty.append(pattern)
                resolved.extend(hits)
            else:
                resolved.append(pattern)
        if empty:
            raise ValueError(
                "crop_source.paths pattern(s) matched no files: "
                + ", ".join(empty)
            )

        # Sorted + de-duplicated so two configs listing the same slides in a
        # different order hash identically -- the crop set is a SET, and
        # param_hash must not depend on how it was typed.
        resolved = sorted(dict.fromkeys(resolved))
        return TiffCropSource(resolved, channel=int(block.get("channel", 0)))

    raise ValueError(
        f"crop_source.kind={kind!r} is not one of 'synthetic', 'institutional'"
    )
