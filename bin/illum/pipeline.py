"""Variant definition + per-variant run (correct all channels, capture metrics)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import time
import numpy as np

from illum.flatfield import estimate_flatfield, apply_field
from illum.darkfield import estimate_dark_constant, estimate_dark_field, subtract_dark
from illum.background import remove_background
from illum.metrics import seam_peak, background_cv, _rss_mb


@dataclass
class Variant:
    name: str
    flatfield: str = "periodic"   # none | periodic | basic
    dark: str = "none"            # none | const | field
    background: str = "none"      # none | opening | gaussian | median | tophat | rolling_ball
    smooth_frac: float = 0.12
    est_downsample: int = 4
    float_pitch: bool = True      # False = verbatim int-pitch tiling (drifts on big mosaics)
    bg_kwargs: Optional[dict] = None


def _grid_for(grid, float_pitch):
    """Round pitch to int for the verbatim (drift-prone) behavior; else pass through."""
    if float_pitch:
        return grid
    g = dict(grid)
    g["pitch_y"] = float(round(grid["pitch_y"]))
    g["pitch_x"] = float(round(grid["pitch_x"]))
    return g


def _correct_channel(ch, grid, variant, out_dtype):
    if variant.flatfield == "basic" and variant.dark != "none":
        raise ValueError("flatfield='basic' does its own darkfield estimation; "
                         "use dark='none' with basic")
    grid = _grid_for(grid, variant.float_pitch)
    f = ch.astype(np.float32)
    # darkfield (additive) subtracted BEFORE the divide
    if variant.dark == "const":
        f = subtract_dark(f, estimate_dark_constant(ch))
    elif variant.dark == "field":
        df = estimate_dark_field(ch, grid, est_downsample=variant.est_downsample)
        f = subtract_dark(f, df, grid=grid)

    ff = None
    if variant.flatfield == "periodic":
        ff = estimate_flatfield(ch, grid, smooth_frac=variant.smooth_frac,
                                est_downsample=variant.est_downsample)
        # apply on the dark-subtracted signal; clip handled inside apply
        f = np.clip(f, 0, None)
        corr = apply_field(f.astype(np.float32), ff, grid, out_dtype=out_dtype)
    elif variant.flatfield == "basic":
        corr = _basic_correct(ch, out_dtype)   # may raise if basicpy absent
    else:  # none
        info = np.iinfo(out_dtype)
        corr = np.clip(f, info.min, info.max).astype(out_dtype)

    if variant.background != "none":
        corr = remove_background(corr, variant.background, out_dtype=out_dtype,
                                 **(variant.bg_kwargs or {}))
    return corr, ff


def _basic_correct(ch, out_dtype):
    import pathlib, sys
    bin_dir = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(bin_dir))
    from preprocess import apply_basic_correction   # requires basicpy
    corrected, _ = apply_basic_correction(ch)
    info = np.iinfo(out_dtype)
    return np.clip(corrected, info.min, info.max).astype(out_dtype)


def run_variant(stack, grid, variant, out_dtype=np.uint16):
    t0 = time.perf_counter()
    r0 = _rss_mb()
    C = stack.shape[0]
    out = np.empty_like(stack, dtype=out_dtype)
    flats = []
    seam_x_b = seam_y_b = cv_b = 0.0
    seam_x_a = seam_y_a = cv_a = 0.0
    for c in range(C):
        ch = stack[c]
        seam_x_b += seam_peak(ch, grid, 1); seam_y_b += seam_peak(ch, grid, 0)
        cv_b += background_cv(ch)
        corr, ff = _correct_channel(ch, grid, variant, out_dtype)
        out[c] = corr
        flats.append(ff)
        seam_x_a += seam_peak(corr, grid, 1); seam_y_a += seam_peak(corr, grid, 0)
        cv_a += background_cv(corr)
    metrics = {
        "seam_x_before": seam_x_b / C, "seam_x_after": seam_x_a / C,
        "seam_y_before": seam_y_b / C, "seam_y_after": seam_y_a / C,
        "cv_before": cv_b / C, "cv_after": cv_a / C,
        "seconds": time.perf_counter() - t0,
        "peak_rss_mb": max(_rss_mb() - r0, 0.0),
    }
    return {"name": variant.name, "corrected": out, "flats": flats, "metrics": metrics}


DEFAULT_MATRIX = [
    Variant("periodic-int-verbatim", flatfield="periodic", dark="none", float_pitch=False),
    Variant("periodic-float", flatfield="periodic", dark="none", float_pitch=True),
    Variant("periodic-float-const-dark", flatfield="periodic", dark="const"),
    Variant("periodic-float-const-dark+gaussian-bg", flatfield="periodic", dark="const",
            background="gaussian", bg_kwargs={"sigma": 50}),
    Variant("periodic-float-field-dark", flatfield="periodic", dark="field"),
]


def full_matrix(available_bg):
    variants = []
    for ff in ("periodic",):
        for dk in ("none", "const", "field"):
            for bg in ["none"] + [b for b in available_bg if b != "none"]:
                name = f"{ff}_{dk}_{bg}"
                variants.append(Variant(name, flatfield=ff, dark=dk, background=bg))
    return variants
