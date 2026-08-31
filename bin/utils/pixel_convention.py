"""The one raster pixel-addressing rule.

A raster mask can be addressed two ways, and MIRAGE publishes artifacts in
both, on purpose:

* **Centre-of-pixel** -- skimage's convention, and the one ``regionprops``
  reports centroids in. Integer ``(row, col)`` names the *centre* of that
  pixel, so a pixel spans ``[r - 0.5, r + 0.5] x [c - 0.5, c + 0.5]``.
* **Corner-of-pixel** -- QuPath's and ImageJ's convention. Integer ``(x, y)``
  names the pixel's *top-left corner*, so a pixel spans ``[x, x + 1] x
  [y, y + 1]``.

The conversion is a half-pixel translation in both axes::

    corner = centre + CORNER_OFFSET

Half a pixel sounds ignorable and is not. It is a *systematic* shift, not
noise: mixing the two conventions inside one artifact puts every cell's point
half a pixel up-and-left of its own polygon, so a neighbourhood analysis run on
the points disagrees with the same analysis run on the shapes, and a
point-in-polygon self-test can fail outright for a small or thin cell.

Who applies it, and who deliberately does not
---------------------------------------------

===========================================  ============  ==========================
site                                         convention    consumer
===========================================  ============  ==========================
``extract_cell_properties.extract_contours``  corner        QuPath (via GeoJSON) and
                                                            the SpatialData shapes
``export_geojson`` centroids (x3)             corner        QuPath ``cells.geojson``
``export_spatialdata`` ``obsm["spatial"]``    corner        SpatialData ``.zarr``
``mask_to_geojson``                           **centre**    VALIS ``warp_geojson``
===========================================  ============  ==========================

``bin/mask_to_geojson.py`` is the deliberate opt-out and must stay that way.
Its contours are fed to VALIS's ``Slide.warp_geojson`` for registration QC, and
VALIS addresses the raster the same way skimage does. Adding the offset there
would bias every warped QC contour by half a pixel against the transform it is
being measured with. That is why the rule lives here rather than as a "+0.5
everywhere" habit: the *choice* per consumer is the interesting part, and one
of the four sites chooses differently.

``bin/join_flowpath.py`` applies the inverse (``/ pixel_size - 0.5``) when it
maps FlowPath's micrometre centroids back onto table rows, and is exact only
because ``export_geojson`` wrote them through this rule in the first place.

Import convention: flat (``from pixel_convention import ...``) by scripts that
``sys.path.insert(0, .../bin/utils)`` first, matching ``measurements.py``,
``pixel_size.py`` and ``image_utils.py``.
"""

from __future__ import annotations

#: Distance from a pixel's centre to its top-left corner, in pixels, per axis.
CORNER_OFFSET = 0.5


def centre_to_corner(coords):
    """Translate centre-of-pixel coordinates into corner-of-pixel ones.

    Parameters
    ----------
    coords
        Anything that supports ``+ float``: a Python scalar, a numpy array of
        any shape, or a pandas object. The offset is per-axis and identical on
        every axis, so an ``(N, 2)`` array of ``(x, y)`` (or ``(y, x)``) pairs
        can be passed whole -- there is nothing to get the axis order wrong.

    Returns
    -------
    The same kind of object, translated by :data:`CORNER_OFFSET`. Never in
    place: an input array is left untouched.
    """
    return coords + CORNER_OFFSET
