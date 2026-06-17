"""ACROBAT landmark adapter.

ACROBAT provides multi-annotator landmark pairs (moving = IHC, target = H&E),
with error reported in µm via microns-per-pixel (mpp). Multiple annotators per
landmark are averaged.

NOTE: column names below follow the public challenge description and may need a
one-line tweak to match your downloaded CSV (see COLS). pair grouping is by
`pair_id`, landmark identity by `point_id`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..landmarks import LandmarkPair

COLS = dict(pair="pair_id", point="point_id", mpp="mpp",
            mov_x="x_ihc", mov_y="y_ihc", tgt_x="x_he", tgt_y="y_he")


def load_pairs(csv_path) -> list[LandmarkPair]:
    df = pd.read_csv(csv_path)
    pairs: list[LandmarkPair] = []
    for pid, g in df.groupby(COLS["pair"], sort=True):
        agg = g.groupby(COLS["point"], sort=True).mean(numeric_only=True)
        moving = agg[[COLS["mov_x"], COLS["mov_y"]]].to_numpy(dtype=float)
        target = agg[[COLS["tgt_x"], COLS["tgt_y"]]].to_numpy(dtype=float)
        mpp = float(agg[COLS["mpp"]].iloc[0])
        pairs.append(LandmarkPair(moving_xy=moving, target_xy=target,
                                  pair_id=str(pid), meta={"mpp": mpp}))
    return pairs
