"""The ±0.5 px convention: one owner, and the three artifacts that depend on it.

Two pixel-coordinate conventions coexist in this pipeline and neither is wrong:

* **skimage / ``regionprops`` pixel-CENTRE** — pixel ``(0, 0)`` has its centre at
  ``0.0``. This is what ``regionprops_table`` centroids and ``find_contours``
  traces are natively in, and what the SpatialData/AnnData ecosystem expects.
* **QuPath / ImageJ pixel-CORNER** — pixel ``(0, 0)`` has its *top-left corner*
  at ``0.0``, so the same feature sits ``+0.5`` higher in both axes.

The rule this module pins:

* the ``.zarr`` is **internally consistent in pixel-CENTRE** — ``shapes`` and
  ``table.obsm["spatial"]`` describe the same cell at the same coordinates;
* the QuPath GeoJSON stays in pixel-CORNER, because QuPath is its consumer;
* ``join_flowpath.py``'s centroid fallback inverts FlowPath's µm centroids back
  into pixel-CENTRE, so it must keep landing on ``obsm["spatial"]``.

The third bullet is the trap: "fixing" the ``.zarr`` by adding ``+0.5`` to
``obsm["spatial"]`` (instead of removing it from the shapes) would make the
store self-consistent *and* silently put a systematic half-pixel offset into the
FlowPath join. ``test_centroid_join_survives_the_half_pixel_trap`` exists to
fail if anyone does that.

``geopandas``/``shapely``/``spatialdata`` live only in the export container, so
the SpatialData element builders are exercised here against minimal stand-ins:
``build_shapes``/``build_table`` are the *real* functions, only their container
-only dependencies are substituted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(BIN / "utils"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import pixel_convention as pc  # noqa: E402

esd = _load("export_spatialdata")
ecp = _load("extract_cell_properties")
egj = _load("export_geojson")
jf = _load("join_flowpath")

PIXEL_SIZE = 0.325
MARKER = "CD3: Nucleus: Median"


# ── container-only dependency stand-ins ────────────────────────────────────────
class _FakePolygon:
    """Just enough shapely: keeps the shell it was built from."""

    def __init__(self, shell):
        self.shell = np.asarray(shell, dtype=float)


@pytest.fixture
def geo_stubs(monkeypatch):
    """Substitute geopandas/shapely/spatialdata, which are container-only."""
    geom = types.ModuleType("shapely.geometry")
    geom.Polygon = _FakePolygon
    shapely = types.ModuleType("shapely")
    shapely.geometry = geom

    gpd = types.ModuleType("geopandas")
    gpd.GeoDataFrame = pd.DataFrame  # a GeoDataFrame is a DataFrame for our purposes

    class _TableModel:
        @staticmethod
        def parse(adata, **_kw):
            return adata

    models = types.ModuleType("spatialdata.models")
    models.TableModel = _TableModel
    sd = types.ModuleType("spatialdata")
    sd.models = models

    for name, mod in (
        ("shapely", shapely),
        ("shapely.geometry", geom),
        ("geopandas", gpd),
        ("spatialdata", sd),
        ("spatialdata.models", models),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


def _polygon_centroid(shell: np.ndarray) -> np.ndarray:
    """Shoelace centroid of a closed ring of ``(x, y)`` vertices."""
    p = np.asarray(shell, dtype=float)
    if not np.array_equal(p[0], p[-1]):
        p = np.vstack([p, p[:1]])
    x, y = p[:, 0], p[:, 1]
    cross = x[:-1] * y[1:] - x[1:] * y[:-1]
    a = 0.5 * cross.sum()
    assert a != 0, "degenerate test polygon"
    return np.array(
        [
            ((x[:-1] + x[1:]) * cross).sum() / (6.0 * a),
            ((y[:-1] + y[1:]) * cross).sum() / (6.0 * a),
        ]
    )


# ── the artifacts, built by the real producers ─────────────────────────────────
@pytest.fixture
def artifacts(tmp_path):
    """Real ``contours.json`` + quant CSV from a two-square synthetic mask.

    Squares are used because a square's polygon centroid and its ``regionprops``
    centroid coincide exactly, so any disagreement is convention, not geometry.
    """
    mask = np.zeros((24, 24), dtype=np.int32)
    mask[4:9, 4:9] = 1  # centre at (y=6.0, x=6.0)
    mask[14:19, 14:19] = 2  # centre at (y=16.0, x=16.0)

    props_df, mask_f, _labels = ecp.extract_morphology(mask)
    contours = ecp.extract_contours(mask_f, props_df, simplify_tolerance=0.1)

    contours_json = tmp_path / "contours.json"
    contours_json.write_text(json.dumps(contours))

    quant = props_df.reset_index()[["label", "y", "x", "area"]].copy()
    quant[MARKER] = [100.0, 200.0]
    quant_csv = tmp_path / "quant.csv"
    quant.to_csv(quant_csv, index=False)

    return types.SimpleNamespace(
        contours_json=str(contours_json),
        quant_csv=str(quant_csv),
        labels=quant["label"].to_numpy(),
        quant=quant,
    )


def _table(artifacts):
    return esd.build_table(
        artifacts.quant_csv,
        "P1",
        None,
        {},
        {"qc_json": "{}", "versions": ""},
        {},
    )


# ── 0. the shared conversion itself ────────────────────────────────────────────
def test_conversions_are_inverse_and_named_by_direction():
    assert pc.centre_to_corner(6.0) == 6.5
    assert pc.corner_to_centre(6.5) == 6.0
    assert pc.corner_to_centre(pc.centre_to_corner(3.25)) == 3.25


def test_scalar_input_stays_scalar():
    assert isinstance(pc.centre_to_corner(1), float)
    assert isinstance(pc.corner_to_centre(1.0), float)


def test_conversions_accept_rings_and_point_arrays():
    ring = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 0.0]]
    np.testing.assert_allclose(pc.centre_to_corner(ring), np.asarray(ring) + 0.5)
    pts = np.array([[1.0, 2.0], [3.0, 4.0]])
    np.testing.assert_allclose(pc.corner_to_centre(pts), pts - 0.5)


def test_keep_centre_is_the_identity_and_does_not_copy():
    ring = [[0.0, 0.0], [1.0, 1.0]]
    assert pc.keep_centre(ring) is ring


# ── 1. the central invariant: one cell, one position ───────────────────────────
def test_zarr_shapes_and_spatial_agree_for_the_same_cell(artifacts, geo_stubs):
    """Inside one ``.zarr``, a cell's polygon and its centroid must coincide."""
    shapes = esd.build_shapes(artifacts.contours_json, artifacts.labels, "cells")
    table = _table(artifacts)

    label_to_row = {int(v): i for i, v in enumerate(table.obs["label"].to_numpy())}

    for label in artifacts.labels:
        poly_centroid = _polygon_centroid(shapes.loc[int(label), "geometry"].shell)
        spatial = table.obsm["spatial"][label_to_row[int(label)]]
        np.testing.assert_allclose(
            poly_centroid,
            spatial,
            atol=1e-9,
            err_msg=f"shapes and obsm['spatial'] disagree for label {label}",
        )


def test_zarr_is_in_the_skimage_pixel_centre_convention(artifacts, geo_stubs):
    """Both elements sit on the *regionprops* centroid, not the QuPath corner."""
    shapes = esd.build_shapes(artifacts.contours_json, artifacts.labels, "cells")
    table = _table(artifacts)

    np.testing.assert_allclose(
        table.obsm["spatial"], artifacts.quant[["x", "y"]].to_numpy(), atol=1e-9
    )
    np.testing.assert_allclose(
        _polygon_centroid(shapes.loc[1, "geometry"].shell), [6.0, 6.0], atol=1e-9
    )


# ── 2. THE TRAP: the FlowPath centroid join reads obsm["spatial"] ──────────────
def _flowpath_frame(centres_px: np.ndarray) -> pd.DataFrame:
    """FlowPath's CSV: µm centroids re-exported from MIRAGE's own GeoJSON.

    MIRAGE writes ``Centroid X µm = (x_px + 0.5) * pixel_size`` (pixel-CORNER,
    QuPath's convention), which is exactly what ``join_by_centroid`` inverts.
    """
    return pd.DataFrame(
        {
            "centroid_x": (centres_px[:, 0] + 0.5) * PIXEL_SIZE,
            "centroid_y": (centres_px[:, 1] + 0.5) * PIXEL_SIZE,
        }
    )


def test_centroid_join_survives_the_half_pixel_trap(tmp_path, geo_stubs):
    """A +0.5 shift of ``obsm["spatial"]`` would re-pair these cells. It must not.

    Two FlowPath cells sit at pixel-centre x = 0.3 and 0.6; the one table cell
    sits at x = 0.0. Nearest is the 0.3 cell. Shift the table cell to 0.5 — the
    naive "make the zarr consistent by adding +0.5 to the centroids" fix — and
    the nearest becomes the 0.6 cell instead. The join is fed straight from
    ``build_table``, so this test fails if that fix is ever made.
    """
    quant = pd.DataFrame(
        {"label": [1], "y": [0.0], "x": [0.0], "area": [9.0], MARKER: [42.0]}
    )
    quant_csv = tmp_path / "quant.csv"
    quant.to_csv(quant_csv, index=False)
    table = esd.build_table(
        str(quant_csv), "P1", None, {}, {"qc_json": "{}", "versions": ""}, {}
    )

    flow = _flowpath_frame(np.array([[0.3, 0.0], [0.6, 0.0]]))
    idx, stats = jf.join_by_centroid(
        flow, table.obsm["spatial"], PIXEL_SIZE, max_dist_px=5.0
    )
    assert stats["method"] == "centroid"
    assert idx.tolist() == [0], (
        "the table cell must match the FlowPath cell at 0.3 px; matching 0.6 px "
        "means a half-pixel offset has been introduced into obsm['spatial']"
    )

    # The case is genuinely 0.5-px-sensitive: shifting the table cell flips it.
    shifted, _ = jf.join_by_centroid(
        flow, table.obsm["spatial"] + 0.5, PIXEL_SIZE, max_dist_px=5.0
    )
    assert shifted.tolist() == [1]


# ── 3. the cross-repo GeoJSON contract keeps its QuPath +0.5 ───────────────────
def test_geojson_centroid_measurements_keep_the_qupath_corner_offset():
    row = pd.Series({"label": 1, "x": 6.0, "y": 6.0, MARKER: 1.0})
    ms = {
        m["name"]: m["value"]
        for m in egj.build_measurements(row, [MARKER], PIXEL_SIZE)
    }
    # build_measurements rounds µm to 3 dp; the offset is +0.5 px, not +0.
    assert ms["Centroid X µm"] == round(6.5 * PIXEL_SIZE, 3)
    assert ms["Centroid Y µm"] == round(6.5 * PIXEL_SIZE, 3)
    assert ms["Centroid X µm"] != round(6.0 * PIXEL_SIZE, 3)


def test_geojson_point_fallback_keeps_the_qupath_corner_offset(tmp_path):
    df = pd.DataFrame({"label": [1], "x": [6.0], "y": [6.0], MARKER: [1.0]})
    out = tmp_path / "cells.geojson"
    egj.export_geojson(df, str(out), PIXEL_SIZE, [MARKER], contours=None)
    feat = json.loads(out.read_text())["features"][0]
    assert feat["geometry"]["type"] == "Point"
    assert feat["geometry"]["coordinates"] == [6.5, 6.5]


def test_contours_json_stays_in_the_qupath_corner_convention(artifacts):
    """``contours.json`` feeds QuPath via ``export_geojson``; it keeps the +0.5.

    Only the ``.zarr``'s copy of those rings is converted back to pixel-centre.
    """
    contours = json.loads(Path(artifacts.contours_json).read_text())
    ring = np.asarray(contours["1"], dtype=float)
    np.testing.assert_allclose(_polygon_centroid(ring), [6.5, 6.5], atol=1e-9)
