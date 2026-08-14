"""Deterministic per-phenotype color palette (phenotyping §4).

Assigns each phenotype a distinct RGB color by spacing hues evenly around the
HSV wheel in input order, so the same phenotype list always yields the same
colors (no RNG, no dependence on set/dict iteration order beyond the caller's
own list ordering). Merged with the reserved colors used for the fixed
non-phenotype classification outcomes (Ambiguous/Conflict/Artefact/
Unclassified) that every compiled panel emits regardless of its own
phenotypes.
"""

from __future__ import annotations

import colorsys
from typing import Dict, List

from .classify import OUTCOME_NAMES

# Colours only -- the NAMES are owned by classify.OUTCOME_NAMES.
_RESERVED_RGB = ([150, 150, 150], [230, 140, 0], [220, 50, 50], [120, 120, 120])
RESERVED = {n: list(rgb) for n, rgb in zip(OUTCOME_NAMES, _RESERVED_RGB)}


def build_palette(phenotype_names: List[str]) -> Dict[str, List[int]]:
    """Build a deterministic RGB palette: one distinct color per phenotype
    name (evenly spaced hues) plus the fixed ``RESERVED`` colors."""
    pal: Dict[str, List[int]] = {}
    names = list(phenotype_names)
    n = max(1, len(names))
    for i, name in enumerate(names):
        h = (i / n) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.95)
        pal[name] = [int(round(r * 255)), int(round(g * 255)), int(round(b * 255))]
    pal.update({k: list(v) for k, v in RESERVED.items()})
    return pal
