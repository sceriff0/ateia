"""ANHIR landmark adapter.

ANHIR ships one CSV per image with columns `,X,Y` (unnamed index, X, Y) in
pixel coordinates. A pair = source CSV + target CSV with row-aligned landmarks.
Error is reported as rTRE = TRE / image diagonal (handled in landmarks.summarize).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..landmarks import LandmarkPair


def load_landmarks(csv_path) -> np.ndarray:
    df = pd.read_csv(csv_path)
    return df[["X", "Y"]].to_numpy(dtype=float)


def _validate_aligned(moving: np.ndarray, target: np.ndarray) -> None:
    if moving.shape != target.shape:
        raise ValueError(f"landmark count mismatch: {moving.shape} vs {target.shape}")


def load_pair(source_csv, target_csv, pair_id: str = "") -> LandmarkPair:
    moving = load_landmarks(source_csv)
    target = load_landmarks(target_csv)
    _validate_aligned(moving, target)
    return LandmarkPair(moving_xy=moving, target_xy=target,
                        pair_id=pair_id or Path(source_csv).stem)
