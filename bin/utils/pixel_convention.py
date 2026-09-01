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

Who works in which convention
-----------------------------

The first three rows CONVERT; the last three merely have to know. Getting a
row wrong is silent in every case.

===========================================  ============  ==========================
site                                         convention    consumer
===========================================  ============  ==========================
``extract_cell_properties.extract_contours``  corner        QuPath (via GeoJSON) and
                                                            the SpatialData shapes
``export_geojson`` centroids (x3)             corner        QuPath ``cells.geojson``
``export_spatialdata`` ``obsm["spatial"]``    corner        SpatialData ``.zarr``
``mask_to_geojson``                           **centre**    VALIS ``warp_geojson``
``join_reg_residuals`` (both sides)           **centre**    an internal QC join
``join_flowpath.join_by_centroid``            corner        an internal cohort join
===========================================  ============  ==========================

``bin/mask_to_geojson.py`` is the deliberate opt-out and must stay that way.
Its contours are fed to VALIS's ``Slide.warp_geojson`` for registration QC, and
VALIS addresses the raster the same way skimage does. Adding the offset there
would bias every warped QC contour by half a pixel against the transform it is
being measured with. That is why the rule lives here rather than as a "+0.5
everywhere" habit: the *choice* per consumer is the interesting part, and not
every site chooses the same way.

The strongest argument for keeping that opt-out is a pairing that never
mentions this module: ``export_spatialdata.join_reg_residuals`` matches
``warp_seg_qc``'s residual centroids -- which trace back to ``mask_to_geojson``
rings, and so are **centre** -- against ``quant[["x", "y"]]``, which is raw
regionprops output and so is **centre** too. It reads the quantification CSV
directly rather than ``obsm["spatial"]``, so it is untouched by the conversion
above. That join is correct, needs no offset, and would acquire a systematic
0.707 px bias the day somebody "fixed" ``mask_to_geojson`` for consistency.

Nothing converts back. ``bin/join_flowpath.py`` matches FlowPath's micrometre
centroids against the store's ``obsm["spatial"]``; both are corner, so its
inverse is a plain ``/ pixel_size``. It used to subtract 0.5 -- correct only
while ``obsm["spatial"]`` was still centre, and a constant 0.707 px bias the
moment that was normalised. If you find yourself writing ``- 0.5``, check which
convention the OTHER side is in first.

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
