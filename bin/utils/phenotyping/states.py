"""Three-valued state-marker annotation (phenotyping section 5.7).

State markers reuse the same per-marker calibration (calibration.py),
conformal scores (conformal.py), and sign resolution (`resolve_sign` /
`resolve_signs` in conformal.py) as ordinary panel markers, evaluated at the
CRC-chosen alpha. The only difference is the final mapping: instead of the
compiler's two-valued pos/neg semantics, state markers collapse to three
values -- "pos" -> +1, "neg" -> -1, and everything else ("free", "contra")
-> 0 (unknown). State markers never enter `F`, the symbolic feasible-vector
compiler; they are reported alongside it.
"""

from __future__ import annotations

import numpy as np

_MAP = {"pos": 1, "neg": -1}


def state_annotation(sign: str) -> int:
    """Map a resolved sign string to its three-valued state annotation."""
    return _MAP.get(sign, 0)


def state_annotations(signs: np.ndarray) -> np.ndarray:
    """Vectorized `state_annotation` over an array of sign strings."""
    return np.array([_MAP.get(str(s), 0) for s in signs], dtype=int)
