import importlib.util
from pathlib import Path

import numpy as np
import tifffile

_SPEC = importlib.util.spec_from_file_location(
    "merge_channels_pyramid",
    Path(__file__).resolve().parent.parent / "bin" / "merge_channels_pyramid.py",
)
mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp)


def test_second_series_holds_uint32_masks(tmp_path):
    intens = np.random.randint(0, 4000, size=(3, 128, 128), dtype=np.uint16)
    masks = np.stack([
        np.random.randint(0, 100000, size=(128, 128), dtype=np.uint32),  # cell (>65535)
        np.random.randint(0, 100000, size=(128, 128), dtype=np.uint32),  # nuclei
    ])
    out = tmp_path / "pyr.ome.tiff"
    mcp.write_pyramidal_ome_tiff(
        intens, str(out),
        channel_names=['DAPI', 'CD8', 'PANCK'],
        channel_colors=[(0, 0, 255), (255, 0, 0), (0, 255, 0)],
        mask_stack=masks, mask_names=['cell_mask', 'nuclei_mask'],
        pyramid_resolutions=3,
    )
    with tifffile.TiffFile(out) as tif:
        assert len(tif.series) == 2
        assert tif.series[0].dtype == np.uint16       # intensities untouched
        s1 = tif.series[1].asarray()
        assert s1.dtype == np.uint32 and s1.shape == (2, 128, 128)
        np.testing.assert_array_equal(s1, masks)       # labels preserved exactly


def test_no_mask_stack_writes_single_series(tmp_path):
    intens = np.random.randint(0, 4000, size=(2, 64, 64), dtype=np.uint16)
    out = tmp_path / "pyr_nomask.ome.tiff"
    mcp.write_pyramidal_ome_tiff(
        intens, str(out),
        channel_names=['DAPI', 'CD8'],
        channel_colors=[(0, 0, 255), (255, 0, 0)],
        mask_stack=None, mask_names=None,
        pyramid_resolutions=3,
    )
    with tifffile.TiffFile(out) as tif:
        assert len(tif.series) == 1                    # unchanged behavior
