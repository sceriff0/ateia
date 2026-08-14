"""Symbolic negative/positive reference resolution (phenotyping §4.5).

Resolves each marker's negative and positive conformal-calibration SOURCE
symbolically, at compile time -- the actual reference cells are selected
later, at runtime, from whichever source this module names. ``auto``
resolution favors panel-implied structure over a blind GMM fallback:

- neg: a ``never`` exclusivity partner of the marker (mutually-exclusive
  markers are each other's best negative population).
- pos: the antecedent ``X`` of a ``requires`` constraint ``X -> marker``
  (cells already gated positive for the antecedent are a good positive
  population for the consequent).

An explicit, non-"auto" ``negative_reference``/``positive_reference`` wins
over ``auto`` inference. It resolves to a ``phenotype`` source if it names a
declared phenotype, otherwise it is kept as a literal ``control`` channel
name (e.g. an isotype control channel).

Unresolved ``auto`` (no structural anchor available) falls back to
``{"type": "gmm"}`` -- this is intentional per the design's two-sided
calibration decision -- and is reported as a warning, never a compile
error: a missing reference is degraded service, not a panel error.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .panel_schema import Panel


def _explicit_source(panel: Panel, ref: str) -> dict:
    if ref in panel.phenotypes:
        return {"type": "phenotype", "name": ref}
    return {"type": "control", "ref": ref}


def _neg_source(panel: Panel, marker: str, never_pairs: List[dict]) -> Tuple[dict, bool]:
    ref = panel.markers[marker].negative_reference
    if ref and ref != "auto":
        return _explicit_source(panel, ref), True
    for pair in never_pairs:
        a, b = pair["markers"]
        if marker == a:
            return {"type": "partner", "marker": b}, True
        if marker == b:
            return {"type": "partner", "marker": a}, True
    # requires contrapositive (Fix 1): if `marker -> consequent`, then
    # consequent-negative cells are necessarily marker-negative, a structural
    # negative anchor that never thresholds `marker` on itself.
    for antecedent, consequent in panel.requires:
        if antecedent == marker:
            return {"type": "requires_neg", "marker": consequent}, True
    return {"type": "gmm"}, False


def _pos_source(panel: Panel, marker: str) -> Tuple[dict, bool]:
    ref = panel.markers[marker].positive_reference
    if ref and ref != "auto":
        return _explicit_source(panel, ref), True
    for antecedent, consequent in panel.requires:
        if consequent == marker:
            return {"type": "requires", "marker": antecedent}, True
    return {"type": "gmm"}, False


def resolve_references(panel: Panel, split: Dict) -> Tuple[Dict[str, dict], List[str]]:
    """Resolve each marker's neg/pos calibration source symbolically.

    Returns ``(references, warnings)`` where ``references[marker]`` is
    ``{"neg_source": {...}, "pos_source": {...}}`` and ``warnings`` names
    every marker/direction that fell back to GMM from an unresolved
    ``auto``.
    """
    never_pairs = split["never"]
    warnings: List[str] = []
    references: Dict[str, dict] = {}
    for name in panel.markers:
        neg, neg_resolved = _neg_source(panel, name, never_pairs)
        pos, pos_resolved = _pos_source(panel, name)
        if not neg_resolved:
            warnings.append(
                f"Marker {name!r}: negative_reference unresolved (auto) -> GMM fallback."
            )
        if not pos_resolved:
            warnings.append(
                f"Marker {name!r}: positive_reference unresolved (auto) -> GMM fallback."
            )
        references[name] = {"neg_source": neg, "pos_source": pos}
    return references, warnings
