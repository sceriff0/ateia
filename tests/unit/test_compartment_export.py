#!/usr/bin/env python3
"""Unit tests for the dual-segmentation GeoJSON export (M3) and nucleus re-keying."""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Add bin/ to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'bin'))


class TestCompartmentGeoJsonExport:
    """export_geojson.export_compartment_geojsons — combined + separate files."""

    @staticmethod
    def _df():
        return pd.DataFrame({
            'label': [1, 2],
            'x': [10.0, 20.0],
            'y': [10.0, 20.0],
            'area': [100.0, 120.0],
            'CD3: Nucleus: Mean': [100.0, 50.0],
            'CD3: Cytoplasm: Mean': [10.0, 5.0],
            'CD3: Cell: Mean': [24.0, 12.0],
        })

    @staticmethod
    def _contours():
        cell = {
            '1': [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]],
            '2': [[15, 15], [25, 15], [25, 25], [15, 25], [15, 15]],
        }
        # Only cell 1 has a nucleus polygon (re-keyed to cell label).
        nucleus = {'1': [[8, 8], [12, 8], [12, 12], [8, 12], [8, 8]]}
        return cell, nucleus

    def test_three_files_and_counts(self, tmp_path):
        from export_geojson import export_compartment_geojsons

        df = self._df()
        cell_c, nuc_c = self._contours()
        markers = ['CD3: Nucleus: Mean', 'CD3: Cytoplasm: Mean', 'CD3: Cell: Mean']
        counts = export_compartment_geojsons(
            df, str(tmp_path), 0.325, markers, cell_c, nuc_c, prefix='cells',
        )
        assert counts == {'cells': 2, 'nuclei': 1, 'cells_wholecell': 2}
        assert (tmp_path / 'cells.geojson').exists()
        assert (tmp_path / 'nuclei.geojson').exists()
        assert (tmp_path / 'cells_wholecell.geojson').exists()

    def test_combined_cell_object_has_toplevel_nucleusgeometry(self, tmp_path):
        from export_geojson import export_compartment_geojsons

        df = self._df()
        cell_c, nuc_c = self._contours()
        markers = ['CD3: Nucleus: Mean', 'CD3: Cytoplasm: Mean', 'CD3: Cell: Mean']
        export_compartment_geojsons(df, str(tmp_path), 0.325, markers, cell_c, nuc_c)

        combined = json.loads((tmp_path / 'cells.geojson').read_text())
        f0, f1 = combined['features']
        # objectType must be "cell" for QuPath to read the nucleus.
        assert f0['properties']['objectType'] == 'cell'
        # nucleusGeometry is a TOP-LEVEL Feature member (sibling of geometry), not in properties.
        assert 'nucleusGeometry' in f0
        assert 'nucleusGeometry' not in f0['properties']
        assert f0['nucleusGeometry']['type'] == 'Polygon'
        assert f0['geometry']['type'] == 'Polygon'
        # Cell 2 has no nucleus -> no nucleusGeometry, still a valid cell object.
        assert 'nucleusGeometry' not in f1
        assert f1['properties']['objectType'] == 'cell'
        # Compartment measurements carried through.
        names = [m['name'] for m in f0['properties']['measurements']]
        assert 'CD3: Nucleus: Mean' in names and 'CD3: Cell: Mean' in names

    def test_separate_files_geometry_and_type(self, tmp_path):
        from export_geojson import export_compartment_geojsons

        df = self._df()
        cell_c, nuc_c = self._contours()
        markers = ['CD3: Nucleus: Mean', 'CD3: Cytoplasm: Mean', 'CD3: Cell: Mean']
        export_compartment_geojsons(df, str(tmp_path), 0.325, markers, cell_c, nuc_c)

        nuclei = json.loads((tmp_path / 'nuclei.geojson').read_text())
        assert len(nuclei['features']) == 1  # only cell 1 has a nucleus
        assert nuclei['features'][0]['properties']['objectType'] == 'detection'

        wholecell = json.loads((tmp_path / 'cells_wholecell.geojson').read_text())
        assert len(wholecell['features']) == 2
        assert all('nucleusGeometry' not in f for f in wholecell['features'])
        assert wholecell['features'][0]['properties']['objectType'] == 'detection'


class TestNucleusReKeying:
    """extract_cell_properties — pairing nuclei to cells, re-keying contours."""

    def test_pairing_identity_labels(self):
        from extract_cell_properties import pair_labels_to_reference

        nuc = np.zeros((10, 10), dtype=np.int32)
        nuc[2:5, 2:5] = 1
        cell = np.zeros((10, 10), dtype=np.int32)
        cell[1:6, 1:6] = 1  # cell = expanded nucleus, same id (StarDist/CellSAM case)
        assert pair_labels_to_reference(nuc, cell) == {1: 1}

    def test_pairing_independent_labels(self):
        from extract_cell_properties import pair_labels_to_reference

        nuc = np.zeros((10, 10), dtype=np.int32)
        nuc[2:5, 2:5] = 7  # nucleus id 7
        cell = np.zeros((10, 10), dtype=np.int32)
        cell[1:6, 1:6] = 3  # cell id 3 (InstanSeg / independent-mask case)
        assert pair_labels_to_reference(nuc, cell) == {7: 3}

    def test_rekey_maps_nucleus_label_to_cell_label(self):
        from extract_cell_properties import rekey_contours_to_reference

        out = rekey_contours_to_reference({'7': [[1, 1], [2, 2]]}, {7: 3}, {7: 9.0})
        assert out == {'3': [[1, 1], [2, 2]]}

    def test_rekey_largest_nucleus_wins_on_collision(self):
        from extract_cell_properties import rekey_contours_to_reference

        contours = {'7': [[1, 1]], '8': [[2, 2]]}
        out = rekey_contours_to_reference(contours, {7: 3, 8: 3}, {7: 5.0, 8: 20.0})
        assert out == {'3': [[2, 2]]}  # nucleus 8 is larger


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
