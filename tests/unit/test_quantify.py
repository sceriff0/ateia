#!/usr/bin/env python3
"""Unit tests for quantify.py module."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Add bin/ to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "bin"))


class TestQuantificationBasics:
    """Test basic quantification functionality."""

    def test_regionprops_extraction(self):
        """Test that regionprops extraction works correctly."""
        # Create synthetic mask with 2 cells
        mask = np.zeros((100, 100), dtype=np.int32)
        mask[10:30, 10:30] = 1  # Cell 1
        mask[50:80, 50:80] = 2  # Cell 2

        # Create synthetic intensity image
        intensity = np.random.randint(50, 200, size=(100, 100), dtype=np.uint16)

        # Manual regionprops extraction
        from skimage.measure import regionprops_table

        props = regionprops_table(
            mask,
            intensity_image=intensity,
            properties=["label", "area", "centroid", "eccentricity", "mean_intensity"],
        )

        df = pd.DataFrame(props)

        assert len(df) == 2, "Should have 2 cells"
        assert "label" in df.columns
        assert "area" in df.columns
        assert "mean_intensity" in df.columns

        # Cell 1 should be roughly 20x20 = 400 pixels
        cell1 = df[df["label"] == 1].iloc[0]
        assert cell1["area"] == pytest.approx(400, abs=10)

    def test_csv_column_structure(self):
        """Test expected CSV column structure."""
        # Expected columns from quantification
        expected_morphology = [
            "label",
            "y",
            "x",
            "area",
            "eccentricity",
            "perimeter",
            "convex_area",
            "axis_major_length",
            "axis_minor_length",
        ]

        # Mock quantification result
        data = {
            "label": [1, 2, 3],
            "y": [10, 20, 30],
            "x": [15, 25, 35],
            "area": [100, 150, 200],
            "eccentricity": [0.5, 0.6, 0.7],
            "perimeter": [40, 50, 60],
            "convex_area": [105, 155, 205],
            "axis_major_length": [12, 15, 18],
            "axis_minor_length": [8, 10, 12],
            "DAPI": [100, 120, 140],
            "FITC": [200, 220, 240],
        }

        df = pd.DataFrame(data)

        # Check all morphology columns present
        for col in expected_morphology:
            assert col in df.columns, f"Missing column: {col}"

        # Check marker columns present
        assert "DAPI" in df.columns
        assert "FITC" in df.columns


class TestCSVMerging:
    """Test CSV merging logic from MERGE_QUANT_CSVS."""

    def test_inner_join_consistency(self):
        """Test that inner join maintains cell label consistency."""
        # Reference CSV (DAPI)
        ref_df = pd.DataFrame(
            {
                "label": [1, 2, 3, 4],
                "area": [100, 150, 200, 250],
                "DAPI": [100, 120, 140, 160],
            }
        )

        # Other marker CSV (FITC) - missing cell 4
        fitc_df = pd.DataFrame({"label": [1, 2, 3], "FITC": [200, 220, 240]})

        # Merge with inner join
        merged = ref_df.merge(fitc_df, on="label", how="inner")

        # Should only have cells 1, 2, 3
        assert len(merged) == 3
        assert list(merged["label"]) == [1, 2, 3]
        assert "DAPI" in merged.columns
        assert "FITC" in merged.columns

    def test_marker_column_extraction(self):
        """Test extraction of marker columns excluding morphology."""
        df = pd.DataFrame(
            {
                "label": [1, 2, 3],
                "area": [100, 150, 200],
                "perimeter": [40, 50, 60],
                "DAPI": [100, 120, 140],
                "FITC": [200, 220, 240],
                "PANCK": [150, 170, 190],
            }
        )

        # Morphology columns to exclude
        morphology_cols = ["label", "area", "perimeter"]

        # Extract marker columns
        marker_cols = [col for col in df.columns if col not in morphology_cols]

        assert marker_cols == ["DAPI", "FITC", "PANCK"]


class TestCompartmentQuantification:
    """Test per-compartment quantification (Nucleus / Cytoplasm / Cell)."""

    @staticmethod
    def _synthetic():
        """Single cell (label 1) with an inner nucleus given a DIFFERENT label (7).

        The differing label id proves the computation does not rely on the cell
        and nucleus masks sharing label IDs (the InstanSeg / independent-mask case).
        Channel = 100 inside the nucleus, 10 in the surrounding cytoplasm.
        """
        cell_mask = np.zeros((20, 20), dtype=np.int32)
        cell_mask[5:15, 5:15] = 1  # 100 px whole-cell
        nuclei_mask = np.zeros((20, 20), dtype=np.int32)
        nuclei_mask[8:12, 8:12] = 7  # 16 px nucleus, id != cell id
        channel = np.zeros((20, 20), dtype=np.float64)
        channel[5:15, 5:15] = 10.0  # cytoplasm value
        channel[8:12, 8:12] = 100.0  # nucleus value
        return cell_mask, nuclei_mask, channel

    def test_standard_default_is_median(self):
        from quantify import compute_compartment_intensities

        cell_mask, nuclei_mask, channel = self._synthetic()
        df = compute_compartment_intensities(cell_mask, nuclei_mask, channel, "CD3")

        assert len(df) == 1
        row = df.iloc[0]
        assert int(row["label"]) == 1
        # Median is the default statistic — always emitted, no expanded needed.
        # Nuclear region = 16 px @100; cytoplasm = 84 px @10; whole-cell = mix.
        assert row["CD3: Nucleus: Median"] == pytest.approx(100.0)
        assert row["CD3: Cytoplasm: Median"] == pytest.approx(10.0)
        # 84 px @10 outnumber 16 px @100 -> whole-cell median is 10.
        assert row["CD3: Cell: Median"] == pytest.approx(10.0)
        # Bare marker column stays whole-cell MEAN (backward-compat / FlowPath fallback).
        assert row["CD3"] == pytest.approx((16 * 100 + 84 * 10) / 100.0)
        # Standard (non-expanded) mode emits NO Mean/Sum columns — those are expanded-only.
        assert not any(": Mean" in c or ": Sum" in c for c in df.columns)

    def test_cytoplasm_is_cell_minus_nucleus(self):
        """Integrated density: Cell Sum == Nucleus Sum + Cytoplasm Sum."""
        from quantify import compute_compartment_intensities

        cell_mask, nuclei_mask, channel = self._synthetic()
        df = compute_compartment_intensities(
            cell_mask, nuclei_mask, channel, "CD3", statistics=["Median", "Mean", "Sum"]
        )
        row = df.iloc[0]
        assert row["CD3: Cell: Sum"] == pytest.approx(
            row["CD3: Nucleus: Sum"] + row["CD3: Cytoplasm: Sum"]
        )
        assert row["CD3: Nucleus: Sum"] == pytest.approx(1600.0)
        assert row["CD3: Cytoplasm: Sum"] == pytest.approx(840.0)

    def test_expanded_medians(self):
        from quantify import compute_compartment_intensities

        cell_mask, nuclei_mask, channel = self._synthetic()
        df = compute_compartment_intensities(
            cell_mask, nuclei_mask, channel, "CD3", statistics=["Median", "Mean", "Sum"]
        )
        row = df.iloc[0]
        assert row["CD3: Nucleus: Median"] == pytest.approx(100.0)
        assert row["CD3: Cytoplasm: Median"] == pytest.approx(10.0)
        # 84 px @10 outnumber 16 px @100, so the whole-cell median is 10.
        assert row["CD3: Cell: Median"] == pytest.approx(10.0)

    def test_quantify_single_channel_dispatches_to_compartments(self):
        from quantify import quantify_single_channel

        cell_mask, nuclei_mask, channel = self._synthetic()
        # With a nuclei mask -> Nucleus/Cytoplasm compartments added (Median default).
        comp = quantify_single_channel(
            cell_mask, channel, "CD3", nuclei_mask=nuclei_mask
        )
        assert "CD3: Nucleus: Median" in comp.columns
        assert "CD3: Cell: Median" in comp.columns
        # Without a nuclei mask -> whole-cell Cell compartment only, still Median default.
        wholecell = quantify_single_channel(cell_mask, channel, "CD3")
        assert list(wholecell.columns) == ["label", "CD3", "CD3: Cell: Median"]
        assert not any(
            "Nucleus" in c or "Cytoplasm" in c for c in wholecell.columns
        )
        # Bare marker column is whole-cell mean in both paths.
        assert wholecell.iloc[0]["CD3"] == pytest.approx(comp.iloc[0]["CD3"])

    def test_empty_mask_returns_empty(self):
        from quantify import compute_compartment_intensities

        empty = np.zeros((10, 10), dtype=np.int32)
        channel = np.zeros((10, 10), dtype=np.float64)
        df = compute_compartment_intensities(empty, empty, channel, "CD3")
        assert df.empty


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
