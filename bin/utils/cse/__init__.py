"""Vendored subset of CellSegmentationEvaluator v1.5.19 (2D path only).

Upstream: Chen & Murphy, "Evaluation of cell segmentation methods without
reference segmentations", Mol. Biol. Cell 34.6 (2023) ar50.
See LICENSE and NOTICE in this directory. Patched for bit-exact vectorized
performance; see docs/superpowers/plans for the equivalence contract.
"""
from .single_method_eval import single_method_eval

__all__ = ["single_method_eval"]
__cse_upstream_version__ = "1.5.19"
