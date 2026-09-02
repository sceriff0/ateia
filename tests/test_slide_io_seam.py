"""The pixel-writer seam: every writer is declared here, and none may appear without a decision.

The review's Step F opens with "twelve pixel writers with no owner, each re-deciding codec, tile
size, bit depth and where pixel size is recorded, while every reader re-derives what it hopes was
written". Moving them behind one module is a multi-session refactor. What can be done first, and
is what this file does, is make the seam **enforceable where it already is**: every call that
writes pixels is declared below with the decisions it makes, so a new one cannot appear silently
and a changed one shows up as a failing test rather than as a surprise at gigapixel scale.

The measured inventory, at the time of writing:

    writer                          multi-ch  photometric  compression  tile      bigtiff
    convert_image.py                  yes     minisblack   none         2048      yes
    tile_for_basic.py                 yes     minisblack   none         none      yes
    apply_basic_profiles.py           yes     minisblack   zlib         2048      yes
    merge_channels_pyramid.py         yes     minisblack   zstd(param)  tile_size yes
    tiled_stitch.py                   yes     minisblack   none         out_tile  yes
    split_multichannel.py             no      -            zlib         2048      yes
    segment.py / _cellsam / _instanseg no     -            zlib         none      NO
    extract_mask_series.py            no      -            zlib         none      yes
    utils/image_utils.py              generic passed through by the caller

Two inconsistencies that fall straight out of it, recorded rather than fixed here because both
change published bytes:

  * the six segmentation mask writers omit ``bigtiff`` while ``extract_mask_series`` -- writing
    THE SAME masks, read back out of the pyramid -- now sets it;
  * the two largest intermediates, ``convert_image`` and ``tiled_stitch``, are written
    uncompressed -- compression stays out of scope for both. ``convert_image`` USED to be
    untiled too; PERF-PLAN.md measured that an untiled canonical intermediate forecloses
    every windowed read downstream (BaSiC, STARE registration, SPLIT_CHANNELS, QC) at 2%
    wall-clock cost to fix, so it is now tiled at ``CONVERT_TIFF_TILE`` (2048px) -- see
    ``tests/test_convert_streaming_write.py::test_the_write_is_tiled``.

The one rule asserted as a rule, rather than merely recorded, is the multi-channel
``photometric="minisblack"`` precondition -- see below.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# file -> (number of pixel-writing call sites, what it writes)
#
# Keyed by file and COUNT rather than by line number: line numbers drift under unrelated edits
# (see tests/test_compartment_mode_routing.py's re-pinning history), while a count still fails
# the moment a writer is added or removed.
PIXEL_WRITERS = {
    "bin/apply_basic_profiles.py": (
        1,
        "the illumination-corrected multi-channel slide",
    ),
    "bin/convert_image.py": (1, "the converted multi-channel slide"),
    "bin/extract_mask_series.py": (
        1,
        "cell/nuclei masks recovered from a prior pyramid",
    ),
    "bin/merge_channels_pyramid.py": (1, "the published QuPath pyramid"),
    "bin/segment.py": (2, "StarDist cell + nuclei masks"),
    "bin/segment_cellsam.py": (2, "CellSAM cell + nuclei masks"),
    "bin/segment_instantseg.py": (2, "InstanSeg cell + nuclei masks"),
    "bin/split_multichannel.py": (1, "one single-channel plane per marker"),
    "bin/tile_for_basic.py": (
        1,
        "the multi-site CZYX pseudo-FOV stack BASICPY fits on",
    ),
    "bin/tiled_stitch.py": (1, "the STARE registered slide"),
    "bin/utils/image_utils.py": (
        1,
        "generic helper; the caller supplies the decisions",
    ),
    "bin/utils/qc.py": (2, "QC raster output, not a pipeline artifact"),
}

# Writers that emit a (C, H, W) stack. These MUST set photometric="minisblack".
MULTI_CHANNEL_WRITERS = (
    "bin/apply_basic_profiles.py",
    "bin/convert_image.py",
    "bin/tile_for_basic.py",
    "bin/merge_channels_pyramid.py",
    "bin/tiled_stitch.py",
)

_WRITE_CALL = re.compile(r"tifffile\.imwrite\(|TiffWriter\(")


def _writer_sites(rel):
    return len(_WRITE_CALL.findall((REPO / rel).read_text()))


def _all_writer_files():
    found = {}
    for path in sorted((REPO / "bin").rglob("*.py")):
        n = len(_WRITE_CALL.findall(path.read_text()))
        if n:
            found[path.relative_to(REPO).as_posix()] = n
    return found


def test_no_undeclared_pixel_writer_exists():
    """A new writer must be declared with its decisions, not appear silently."""
    found = _all_writer_files()
    undeclared = sorted(set(found) - set(PIXEL_WRITERS))

    assert not undeclared, (
        "these files write pixels but are not declared in PIXEL_WRITERS. Add them with the "
        "decisions they make (codec, tile, bigtiff, photometric), or route them through an "
        "existing writer:\n  " + "\n  ".join(undeclared)
    )


def test_every_declared_writer_still_exists_and_still_writes():
    """A stale declaration is worse than none: it makes the count check pass vacuously."""
    found = _all_writer_files()
    stale = sorted(set(PIXEL_WRITERS) - set(found))

    assert not stale, (
        "these are declared as pixel writers but no longer write pixels; remove the entry:\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.parametrize("rel", sorted(PIXEL_WRITERS))
def test_the_number_of_writers_per_file_is_unchanged(rel):
    expected, what = PIXEL_WRITERS[rel]

    assert _writer_sites(rel) == expected, (
        f"{rel} ({what}) now has {_writer_sites(rel)} pixel-writing call sites, not {expected}. "
        "If that is intended, update PIXEL_WRITERS and state what the new writer decides."
    )


@pytest.mark.parametrize("rel", MULTI_CHANNEL_WRITERS)
def test_every_multi_channel_writer_sets_photometric_minisblack(rel):
    """PERF-PLAN.md Wave 0.3: the precondition the whole per-page read strategy rests on.

    Without `photometric="minisblack"`, tifffile stores a (C, H, W) array as ONE page with C
    samples, and both `key=` and `pages[i]` then raise IndexError. Every "read the page, not the
    plane" optimisation downstream depends on this silently.
    """
    text = (REPO / rel).read_text()

    assert 'photometric="minisblack"' in text, (
        f'{rel} writes a multi-channel stack without photometric="minisblack". tifffile will '
        "store it as one page with C samples and pages[i] will raise IndexError."
    )


def test_minisblack_really_is_what_makes_pages_addressable(tmp_path):
    """Pin the REASON for the rule, so it cannot decay into a style preference.

    This is PERF-PLAN's blocker #2, reproduced: the rule is not cosmetic, and a reader that
    assumes one page per channel is broken by its absence.
    """
    np = pytest.importorskip("numpy")
    tifffile = pytest.importorskip("tifffile")

    data = np.zeros((3, 32, 32), dtype=np.uint16)

    with_flag = tmp_path / "with.tiff"
    without_flag = tmp_path / "without.tiff"
    tifffile.imwrite(str(with_flag), data, photometric="minisblack")
    tifffile.imwrite(str(without_flag), data)

    with tifffile.TiffFile(str(with_flag)) as tif:
        assert len(tif.series[0].pages) == 3, (
            "one page per channel is the property we rely on"
        )
        assert tif.series[0].pages[2].asarray().shape == (32, 32)

    with tifffile.TiffFile(str(without_flag)) as tif:
        n_pages = len(tif.series[0].pages)

    assert n_pages != 3, (
        'writing without photometric="minisblack" produced one page per channel anyway on this '
        "tifffile version, so this test no longer demonstrates why the rule exists. Re-check "
        "against the pinned container version before relaxing anything."
    )


# ---------------------------------------------------------------------------
# The inconsistency the inventory exposed
# ---------------------------------------------------------------------------

MASK_WRITERS = (
    "bin/segment.py",
    "bin/segment_cellsam.py",
    "bin/segment_instantseg.py",
    "bin/extract_mask_series.py",
)


@pytest.mark.parametrize("rel", MASK_WRITERS)
def test_every_mask_writer_compresses(rel):
    """Label masks are long runs of identical integers; compression is close to free.

    PERF-PLAN measures an uncompressed uint32 label mask against zstd-3 at 6.7x the write time,
    8.4x the read time, and vastly larger. There is no trade-off to weigh.
    """
    assert 'compression="zlib"' in (REPO / rel).read_text(), (
        f"{rel} writes a label mask without compression"
    )


@pytest.mark.parametrize("rel", MASK_WRITERS)
def test_every_mask_writer_sets_bigtiff(rel):
    """The same masks, written by four files, must not disagree about the 4 GB ceiling.

    A 40000x40000 uint32 mask is 6.4 GB before compression. `tiled_stitch.py:118` states the
    rule: classic TIFF's 32-bit offsets overflow past 4 GB. Compression usually keeps a label
    mask under it -- usually is not a contract, and a mask with many labels and little run
    structure is exactly the case that compresses worst AND is largest.
    """
    assert "bigtiff=True" in (REPO / rel).read_text(), (
        f"{rel} writes a full-resolution label mask without bigtiff, while its sibling "
        "extract_mask_series.py -- writing the same masks back out of the pyramid -- sets it"
    )


# ---------------------------------------------------------------------------
# The tile/iterator contract — the one this seam did not know about
# ---------------------------------------------------------------------------
#
# tifffile's iterator mode is TILE-wise, not plane-wise, whenever `tile=` is set: it
# walks `numtiles` items per page and raises ValueError('tile is too large') the moment
# one exceeds a single tile. So a writer that passes BOTH `tile=` AND a generator must
# feed that generator TILES.
#
# convert_image.py fed it whole planes. Nothing caught it, in three separate places:
# this seam checks photometric/compression/bigtiff but not the data argument;
# test_convert_streaming_write.py's fixtures are 24x20, smaller than one 2048 tile, so
# tifffile PADDED them and the tiled-write test passed; and `-stub` never runs the
# script at all. It failed on the first real slide (30552 x 32072), at CONVERT_IMAGE,
# after the scheduler had granted the task its memory.
#
# Every generator fed to a tiled write is declared here with what it yields. A new one
# has to be added deliberately, which is the moment to ask whether it yields tiles.
TILE_FED_GENERATORS = {
    "_iter_tiles": "bin/convert_image.py -- wraps _iter_planes and re-slices each plane",
    "_tiles": "bin/apply_basic_profiles.py -- channel-major, tile-major",
    "_plane_tiles": "bin/merge_channels_pyramid.py -- per-plane tile walk",
    "stream_tiles": "bin/tiled_stitch.py -- warps and emits one out_tile at a time",
}


def _tiled_writes_with_a_generator():
    """(file, lineno, callee) for every write that sets tile= and is fed a call."""
    import ast

    out = []
    for rel in sorted(set(PIXEL_WRITERS)):
        path = REPO / rel
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in ("write", "imwrite"):
                continue
            if not any(k.arg == "tile" for k in node.keywords):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Call):
                callee = first.func
                out.append(
                    (
                        rel,
                        node.lineno,
                        callee.attr
                        if isinstance(callee, ast.Attribute)
                        else getattr(callee, "id", "<expr>"),
                    )
                )
    return out


def test_every_generator_fed_to_a_tiled_write_yields_tiles():
    """A tiled write fed a PLANE generator fails only on an image bigger than one
    tile -- i.e. never in this suite, and always in production."""
    undeclared = [
        (f, ln, c)
        for f, ln, c in _tiled_writes_with_a_generator()
        if c not in TILE_FED_GENERATORS
    ]
    assert not undeclared, (
        "tiled write fed an undeclared generator: "
        + ", ".join(f"{f}:{ln} -> {c}()" for f, ln, c in undeclared)
        + ". tifffile requires TILES here, not planes; a plane generator raises "
        "'tile is too large' on any image larger than one tile. Declare it in "
        "TILE_FED_GENERATORS once you have checked what it yields."
    )


def test_the_declaration_is_not_carrying_dead_entries():
    """A name here that no tiled write uses is an excuse for a call that is gone."""
    live = {c for _, _, c in _tiled_writes_with_a_generator()}
    stale = sorted(set(TILE_FED_GENERATORS) - live)
    assert not stale, (
        f"TILE_FED_GENERATORS names generators no tiled write feeds: {stale}"
    )
