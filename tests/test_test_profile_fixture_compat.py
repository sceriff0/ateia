"""The test profile's tiling must stay compatible with the test fixture sizes.

conf/test.config pins preproc_tile_size, and tests/testdata/ holds 128x128 fixtures.
bin/tile_for_basic.py refuses an image that yields fewer than 2 BaSiC FOVs, because
nf-core's basicpy module cannot fit an illumination profile from a single site. The
two numbers live in different files with no mechanical link, so a change to either
can break the real nf-test suite -- which does not run on every push.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST_CONFIG = REPO / "conf" / "test.config"

sys.path.insert(0, str(REPO / "bin"))
from utils.fov_tiling import count_fovs  # noqa: E402

# The synthetic fixtures written by tests/testdata/generate_complete_testdata.py.
FIXTURE_EDGE_PX = 128
# bin/tile_for_basic.py raises when n_tiles < 2.
MIN_FOVS_FOR_BASIC = 2


def _preproc_tile_size():
    text = TEST_CONFIG.read_text()
    m = re.search(r"^\s*preproc_tile_size\s*=\s*(\d+)", text, re.MULTILINE)
    assert m, "conf/test.config does not pin preproc_tile_size"
    return int(m.group(1))


def test_test_profile_tiling_yields_enough_basic_fovs():
    """The pinned tile size must give BaSiC at least two fields on the fixtures.

    count_fovs() is IMPORTED rather than reimplemented here. It ceils
    (bin/utils/fov_tiling.py:129), so a floor-division copy of the arithmetic
    disagrees with it on every tile size that does not divide the fixture edge --
    a guard that rejects tile sizes which actually work.
    """
    tile = _preproc_tile_size()
    n_y, n_x = count_fovs((FIXTURE_EDGE_PX, FIXTURE_EDGE_PX), (tile, tile))
    total = n_y * n_x
    assert total >= MIN_FOVS_FOR_BASIC, (
        f"conf/test.config pins preproc_tile_size={tile}, which tiles a "
        f"{FIXTURE_EDGE_PX}x{FIXTURE_EDGE_PX} fixture into {total} BaSiC FOV(s). "
        f"BaSiC needs at least {MIN_FOVS_FOR_BASIC} (bin/tile_for_basic.py). Lower "
        f"preproc_tile_size to at most {max(1, FIXTURE_EDGE_PX // 2)}."
    )
