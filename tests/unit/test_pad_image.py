#!/usr/bin/env python3
"""Regression test for bin/pad_image.py.

PAD_IMAGES must preserve the input's physical pixel size in the output OME-XML.
Previously it hardcoded PhysicalSize 0.325 um, silently rescaling every downstream
micron measurement for any acquisition not at 0.325 um/px.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "bin"))
pad = pytest.importorskip("pad_image")

NS = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}


def _physical_size_x(path):
    with tifffile.TiffFile(str(path)) as tif:
        desc = tif.ome_metadata or tif.pages[0].description
    # Encode to bytes so an XML declaration (<?xml encoding=…?>) doesn't trip ET.
    root = ET.fromstring(desc.encode("utf-8") if isinstance(desc, str) else desc)
    return root.find(".//ome:Pixels", NS).get("PhysicalSizeX")


def test_pad_preserves_input_pixel_size(tmp_path):
    inp = tmp_path / "in.ome.tiff"
    tifffile.imwrite(
        str(inp),
        np.zeros((2, 32, 32), np.uint16),
        ome=True,
        metadata={
            "axes": "CYX",
            "PhysicalSizeX": 0.5,
            "PhysicalSizeY": 0.5,
            "PhysicalSizeXUnit": "µm",
            "Channel": {"Name": ["DAPI", "PANCK"]},
        },
    )
    out = tmp_path / "out.ome.tiff"
    pad.pad_single_image(inp, out, target_h=48, target_w=48)

    assert _physical_size_x(out) == "0.5", (
        f"pixel size not preserved: got {_physical_size_x(out)} (the old hardcode was 0.325)"
    )


def test_pad_falls_back_when_pixel_size_absent(tmp_path):
    inp = tmp_path / "in.ome.tiff"
    tifffile.imwrite(
        str(inp),
        np.zeros((1, 16, 16), np.uint16),
        ome=True,
        metadata={"axes": "CYX", "Channel": {"Name": ["DAPI"]}},
    )
    out = tmp_path / "out.ome.tiff"
    pad.pad_single_image(inp, out, target_h=24, target_w=24)
    # No input pixel size → fall back to the 0.325 default (never crash).
    assert _physical_size_x(out) == "0.325"
