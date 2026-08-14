"""One owner for the ±0.5 px coordinate-convention shift.

Two pixel-coordinate conventions coexist in this pipeline, and neither is wrong:

``skimage`` / ``regionprops`` **pixel-CENTRE**
    Pixel ``(0, 0)`` has its *centre* at ``0.0``. This is what
    ``regionprops_table`` centroids and ``find_contours`` traces are natively
    in, and what the scverse (SpatialData / AnnData / squidpy) stack expects.

QuPath / ImageJ **pixel-CORNER**
    Pixel ``(0, 0)`` has its *top-left corner* at ``0.0``, so the same feature
    sits ``+0.5`` further along both axes.

Before this module the ``0.5`` was open-coded at six sites across five scripts,
each re-deciding the direction from a comment. The decision was made
inconsistently *inside a single artifact*: the SpatialData ``.zarr`` carried
pixel-CORNER polygons and pixel-CENTRE centroids for the same cell.

Which artifact is in which convention
-------------------------------------

=======================================  ==================
artifact                                 convention
=======================================  ==================
``cells.geojson`` (QuPath / FlowPath)    pixel-CORNER
``contours.json`` (feeds that GeoJSON)   pixel-CORNER
``<patient>.zarr`` shapes **and**        pixel-CENTRE
``obsm["spatial"]``
seg-QC GeoJSON (VALIS ``warp_geojson``)  pixel-CENTRE
=======================================  ==================

``bin/join_flowpath.py`` reads FlowPath's µm centroids -- which came from the
pixel-CORNER GeoJSON -- and converts them back to pixel-CENTRE so they can be
matched against ``obsm["spatial"]``. That inversion and the ``.zarr``'s
convention are the same decision and must move together; they are both spelt
here so they cannot drift apart.

Import convention: flat (``from pixel_convention import ...``) from scripts
that ``sys.path.insert(0, .../bin/utils)`` first, matching ``measurements.py``.
Pure numpy, which every consumer already depends on.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Distance from a pixel's centre to its top-left corner, in pixels.
CENTRE_TO_CORNER_OFFSET: float = 0.5


def _shift(coords: Any, delta: float) -> Any:
    """Add ``delta`` to every coordinate, preserving scalar-ness.

    Accepts a scalar, an ``(x, y)`` pair, an ``(N, 2)`` array of points, or a
    polygon ring as a nested list -- every shape the callers actually hold.
    """
    if np.isscalar(coords):
        return float(coords) + delta
    return np.asarray(coords, dtype=float) + delta


def centre_to_corner(coords: Any) -> Any:
    """skimage pixel-CENTRE -> QuPath/ImageJ pixel-CORNER (``+0.5``).

    Use when writing coordinates into an artifact QuPath will open.
    """
    return _shift(coords, CENTRE_TO_CORNER_OFFSET)


def corner_to_centre(coords: Any) -> Any:
    """QuPath/ImageJ pixel-CORNER -> skimage pixel-CENTRE (``-0.5``).

    Use when reading coordinates that came back from QuPath, or when writing
    them into a scverse artifact.
    """
    return _shift(coords, -CENTRE_TO_CORNER_OFFSET)


def keep_centre(coords: Any) -> Any:
    """Identity: the coordinates stay in skimage pixel-CENTRE.

    Exists so that "no offset here" reads as a decision rather than as an
    omission. The seg-QC GeoJSON is the case: it is traced by ``find_contours``
    and consumed by VALIS's ``warp_geojson``, both of which are pixel-CENTRE,
    so converting it would be the bug.
    """
    return coords
