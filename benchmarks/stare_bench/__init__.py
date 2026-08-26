"""Synthetic ground-truth benchmark for STARE registration.

Generates image pairs whose deformation is known EXACTLY at every pixel, so
registration accuracy can be measured without landmarks and without the
circular intrinsic TRE that bin/tiled_solve.py reports.

__version__ is stamped into every truth.json as `generator_version`. Bump the
MINOR component when generated data changes in a way that invalidates
comparison with older runs; bump PATCH for changes that cannot.
"""

__version__ = "1.0.0"
