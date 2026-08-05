#!/usr/bin/env python3
"""Nuclear/fiducial channel resolution.

The nuclear channel is resolved by marker NAME from metadata (never the filename),
using an ordered preference list. These tests pin the ordering, case-insensitivity,
the no-match contract of the shared helper, and CONVERT_IMAGE's use of it (reorder
to channel 0, fail-fast when absent, single-channel exception).
"""

from pathlib import Path

import numpy as np
import pytest
from utils.metadata import DEFAULT_NUCLEAR_MARKERS, pick_nuclear_index


class TestPickNuclearIndex:
    def test_default_markers_are_dapi_then_celltox(self):
        assert list(DEFAULT_NUCLEAR_MARKERS) == ["DAPI", "CELLTOX"]

    def test_dapi_wins_when_both_present(self):
        # Marker preference (DAPI first) beats channel order.
        assert pick_nuclear_index(["CELLTOX", "CD8", "DAPI"]) == 2

    def test_celltox_used_when_dapi_absent(self):
        assert pick_nuclear_index(["CD8", "CELLTOX", "CD68"]) == 1

    def test_returns_none_when_no_marker_matches(self):
        assert pick_nuclear_index(["CD8", "CD68", "PDL1"]) is None

    def test_case_insensitive_substring_match(self):
        assert pick_nuclear_index(["cd8", "dapi-nuclear"]) == 1

    def test_marker_order_is_honored_over_channel_order(self):
        assert pick_nuclear_index(["DAPI", "CELLTOX"], ["CELLTOX", "DAPI"]) == 1

    def test_empty_or_none_channel_list_returns_none(self):
        assert pick_nuclear_index([]) is None
        assert pick_nuclear_index(None) is None


def _fake_read_image(channel_count):
    """Return a (read_image-compatible) stand-in producing a (C, Y, X) uint16 image."""

    def _reader(_path):
        img = np.zeros((channel_count, 4, 4), dtype=np.uint16)
        for c in range(channel_count):
            img[c] = c + 1
        meta = {
            "original_dims": "CYX",
            "num_channels": channel_count,
            "channel_names_from_file": None,
        }
        return img, meta

    return _reader


class TestConvertImageNuclearResolution:
    def test_dapi_moved_to_channel_zero(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(3))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["CD8", "DAPI", "CD68"]
        )
        assert output_channels[0] == "DAPI"
        assert set(output_channels) == {"CD8", "DAPI", "CD68"}

    def test_celltox_used_when_dapi_absent(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(3))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["CD8", "CELLTOX", "CD68"]
        )
        assert output_channels[0] == "CELLTOX"

    def test_fail_fast_when_no_nuclear_marker_multichannel(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(2))
        with pytest.raises(ValueError, match="nuclear_markers"):
            convert_image.convert_to_ome_tiff(
                Path("in.ome.tif"), tmp_path, "P001", ["CD8", "CD68"]
            )

    def test_single_channel_assumed_nuclear(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(1))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["CD8"]
        )
        assert output_channels == ["CD8"]

    def test_custom_nuclear_markers_override(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(2))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["CD8", "SMA"], nuclear_markers=["SMA"]
        )
        assert output_channels[0] == "SMA"
