"""The one place this pipeline decides how to read an image, and the only place it writes one.

TWO OWNERSHIPS, ONE MODULE.

**Which reader claims a file.** `bin/convert_image.py` used to answer this with three
module-level sets and a silent `return "bioio"` fall-through, so an unrecognised
extension -- a .png dropped into a samplesheet -- was handed to BioImage and failed
several frames inside a plugin, naming a problem that was not the problem.
`detect_reader` answers it from ONE table and raises `UnsupportedFormatError` for
anything unclaimed, with the suffix and the supported list in the message.

**Every TIFF this pipeline writes.** Before this module there was no writer helper at
all: ten scripts called `tifffile.imwrite`/`tifffile.TiffWriter` directly, and the OME
metadata dict (PhysicalSizeX/Y + unit + channel names + axes) was rebuilt independently
in four of them -- plus a fifth, hand-written `<OME ...>` XML template in
`merge_channels_pyramid.py` that nothing ever called. `tests/test_ome_io_is_the_only_writer.py`
now fails on any `tifffile` write call outside this file.

LAZY IMPORTS ARE A HARD REQUIREMENT, NOT A STYLE CHOICE. `requirements/ci.txt` installs
`tifffile`, `numpy`, `zarr` and `dask`, and installs NONE of `bioio`, `bioio-bioformats`
or `h5py` (they belong to `containers/convert` alone). A module-scope `import bioio` here
would break every pytest that imports this module, in CI, at collection time -- silently,
because a module-scope failure deselects the whole file rather than failing it (see
tests/conftest.py's floor). So `tifffile` and `numpy` are the only hard imports; every
vendor-format reader imports its backend INSIDE the function that needs it.

IMPORT CONVENTION. Flat (`from ome_io import ...`) by scripts that
`sys.path.insert(0, .../bin/utils)` first, matching `pixel_size.py`, `tiled_io.py` and
`segment_io.py`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import logging
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np
import tifffile

__all__ = [
    "CONVERT_TIFF_TILE",
    "READERS",
    "UnsupportedFormatError",
    "detect_reader",
    "ome_metadata",
    "ome_tiff_writer",
    "require_reader",
    "write_ome_tiff",
    "write_tiff",
]

logger = logging.getLogger(__name__)


class UnsupportedFormatError(ValueError):
    """Raised by `detect_reader` for an extension no installed reader claims.

    A `ValueError` subclass so a caller that already wraps a read in
    `except ValueError` keeps working, and its own type so a caller can tell
    "this pipeline does not read that format" apart from "this file is corrupt".
    """


#: The reader names `detect_reader` returns and `ImageInfo.reader` reports.
READERS = ("bioio", "bioio-bioformats", "tifffile", "hdf5")

#: suffix -> reader. THE dispatch table; there is no fall-through.
#:
#: `.ome.tif`/`.ome.tiff` are listed separately from `.tif`/`.tiff` even though both
#: resolve to the same reader, because the two-part suffix is the one this pipeline's own
#: intermediates carry and a future split (an OME-specific fast path) must not have to
#: reintroduce the double-suffix parse to make it.
#:
#: The `bioio-bioformats` block is the R2 route: "any Bio-Formats-compatible format",
#: honoured by installing `bioio-bioformats` in `containers/convert` and baking its jars
#: at build time (phase 06). Declaring the route here BEFORE the plugin exists is
#: deliberate -- `require_reader` turns its absence into a message naming the plugin,
#: which is strictly better than a suffix that silently routes somewhere wrong.
_SUFFIX_TO_READER = {
    ".ome.tif": "bioio",
    ".ome.tiff": "bioio",
    ".tif": "bioio",
    ".tiff": "bioio",
    ".nd2": "bioio",
    ".czi": "bioio",
    ".lif": "bioio",
    ".ndpi": "tifffile",
    ".ndpis": "tifffile",
    ".h5": "hdf5",
    ".hdf5": "hdf5",
    ".svs": "bioio-bioformats",
    ".qptiff": "bioio-bioformats",
    ".vsi": "bioio-bioformats",
    ".scn": "bioio-bioformats",
    ".mrxs": "bioio-bioformats",
    ".bif": "bioio-bioformats",
    ".ims": "bioio-bioformats",
}

#: reader -> the importable module names it needs. `bioio-bioformats` needs BOTH the
#: `bioio` core and the `bioio_bioformats` plugin; naming only the plugin would let a
#: half-installed environment fail later and less clearly.
_READER_MODULES = {
    "bioio": ("bioio",),
    "bioio-bioformats": ("bioio", "bioio_bioformats"),
    "tifffile": ("tifffile",),
    "hdf5": ("h5py",),
}

#: pip distribution name per module, for the message. `bioio_bioformats` is installed as
#: `bioio-bioformats`; telling an operator to `pip install bioio_bioformats` works by
#: accident today and is not what the container's requirements file says.
_MODULE_TO_DISTRIBUTION = {
    "bioio": "bioio",
    "bioio_bioformats": "bioio-bioformats",
    "tifffile": "tifffile",
    "h5py": "h5py",
}


def _module_is_importable(name: str) -> bool:
    """Whether `name` can be imported, WITHOUT importing it.

    `importlib.util.find_spec` rather than a try/import: importing `bioio` pulls a
    five-plugin, JVM-adjacent stack, and this is called on a path where the answer is
    usually "no". Exported (no underscore prefix would be nicer, but it is genuinely
    internal) only so tests/test_ome_io.py can ask the same question before asserting on
    the absent-plugin message.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        # find_spec raises for a namespace package whose parent is missing.
        return False


def detect_reader(path) -> str:
    """Which of `READERS` claims `path`, by extension alone.

    Extension alone, deliberately: this is called before the file is opened -- it is what
    DECIDES how to open it -- and content sniffing a vendor format needs the very reader
    this function is choosing. A file whose bytes disagree with its name fails at the
    read, loudly, which is the right place for that failure.

    Raises `UnsupportedFormatError` for anything not in the table. There is no
    fall-through: `convert_image.get_file_format`'s silent `return "bioio"` is the
    behaviour this replaces.
    """
    name = Path(path).name.lower()
    for double in (".ome.tif", ".ome.tiff"):
        if name.endswith(double):
            return _SUFFIX_TO_READER[double]

    suffix = Path(name).suffix
    reader = _SUFFIX_TO_READER.get(suffix)
    if reader is None:
        supported = ", ".join(sorted(_SUFFIX_TO_READER))
        raise UnsupportedFormatError(
            f"{Path(path).name}: no reader claims the extension "
            f"{suffix or '<none>'!r}. Supported extensions: {supported}."
        )
    return reader


def require_reader(reader: str) -> None:
    """Fail now, naming the plugin, if `reader`'s backend is not installed.

    The `.svs/.qptiff/.vsi/.scn/.mrxs/.bif/.ims` route needs `bioio-bioformats`, which
    `containers/convert` installs (with its Bio-Formats jars baked at build time) and no
    other image does. Without this check a run in the wrong image dies with a bare
    `ModuleNotFoundError: No module named 'bioio_bioformats'` several frames inside a
    reader, which does not tell an operator that the fix is the image.
    """
    modules = _READER_MODULES.get(reader)
    if modules is None:
        raise UnsupportedFormatError(
            f"Unknown reader {reader!r}. Valid readers: {', '.join(READERS)}."
        )
    missing = [m for m in modules if not _module_is_importable(m)]
    if missing:
        distributions = ", ".join(_MODULE_TO_DISTRIBUTION[m] for m in missing)
        raise ImportError(
            f"the {reader!r} route needs {distributions}, which is not installed in this "
            f"environment. This format is read by containers/convert "
            f"(bolt3x/mirage-convert), which installs it; a run outside that image, or an "
            f"image built before {distributions} was added to requirements, cannot open it."
        )


#: TIFF tile size (px) for the pipeline's canonical converted intermediate -- a TIFF
#: LAYOUT choice, not a processing tile size (contrast ``params.preproc_tile_size``,
#: BaSiC's pseudo-FOV grid).
#:
#: tifffile's zarr view of a STRIPED (untiled) TIFF reports ``chunks=(1, H, W)``, one
#: chunk per whole plane, so every "read just this region" call downstream -- the BaSiC
#: path, the STARE registration path, SPLIT_CHANNELS, the QC processes -- decodes the
#: entire plane to slice it. Measured on a 6000x6000 uint16 plane: a single 2048^2 region
#: read peaks at 76.7 MiB striped vs 16.0 MiB tiled. CONVERT_IMAGE writes the pipeline's
#: canonical intermediate, so an untiled write there is the origin of that cost for every
#: reader below it. Pinned by ``tests/test_convert_streaming_write.py``.
CONVERT_TIFF_TILE = 2048

#: Above this many bytes a classic TIFF's 32-bit offsets overflow
#: (``struct.error: 'I' format requires 0 <= number <= 4294967295``). 2 GiB rather than
#: 4 GiB because the ceiling applies to the largest OFFSET, not to the payload, and the
#: header/IFD overhead of a tiled multi-page file is not accounted for anywhere else.
BIGTIFF_THRESHOLD_BYTES = 2**31

_MICRON = "µm"


def ome_metadata(
    channels: Optional[Sequence[str]],
    pixel_size_um: Union[float, Tuple[Optional[float], Optional[float]], None],
    *,
    axes: str = "CYX",
    pixel_size_z_um: Optional[float] = None,
) -> dict:
    """The OME metadata dict tifffile serialises into an image's header.

    THIS IS THE FINDING THIS MODULE EXISTS FOR. The same six-key dict was rebuilt
    independently in ``convert_image.py``, ``merge_channels_pyramid.py``,
    ``apply_basic_profiles.py`` and ``tiled_stitch.py`` -- four copies of one convention,
    each free to drift in unit spelling, key order or which keys it omits. A fifth copy,
    ``merge_channels_pyramid.build_ome_xml``, hand-wrote the ``<OME ...>`` XML itself and
    was called by nothing at all.

    KEY ORDER IS PART OF THE OUTPUT. tifffile walks this dict to build the XML, so the
    order below is the attribute order in the header of every file the pipeline writes.
    It is the order ``convert_image.py`` used; changing it is a diff in a published
    artifact for no behavioural reason. ``tests/test_ome_io.py`` pins it.

    ``None`` OMITS RATHER THAN SUBSTITUTES. A missing scale means "the file did not say",
    and inventing one is the exact failure ``bin/utils/pixel_size.py`` exists to prevent
    (its module docstring has the full argument). Same for channel names: an anonymous
    header is honest, a mislabelled one is not.

    ``pixel_size_um`` may be one number (X and Y equal) or an ``(x, y)`` pair -- the
    vendor formats ``convert_image.py`` reads report the two separately and they can
    genuinely differ.
    """
    if isinstance(pixel_size_um, tuple):
        px_x, px_y = pixel_size_um
    else:
        px_x = px_y = pixel_size_um

    md: dict = {"axes": axes}
    if channels:
        md["Channel"] = {"Name": list(channels)}
    if px_x is not None:
        md["PhysicalSizeX"] = px_x
        md["PhysicalSizeXUnit"] = _MICRON
    if px_y is not None:
        md["PhysicalSizeY"] = px_y
        md["PhysicalSizeYUnit"] = _MICRON
    if pixel_size_z_um is not None:
        md["PhysicalSizeZ"] = pixel_size_z_um
        md["PhysicalSizeZUnit"] = _MICRON
    return md


def _wants_bigtiff(bigtiff: Optional[bool], nbytes: int) -> bool:
    """Resolve the tri-state ``bigtiff`` argument.

    ``None`` -- the default -- means "BigTIFF iff the payload exceeds
    ``BIGTIFF_THRESHOLD_BYTES``", so a small file stays a classic TIFF (which every reader
    accepts) and a real whole-slide image does not overflow. ``True``/``False`` are the
    caller asserting, and win: ``bin/convert_image.py`` passes ``True`` unconditionally
    because that is what it has always written and its outputs are compared against a
    reference writer.
    """
    if bigtiff is not None:
        return bool(bigtiff)
    return nbytes > BIGTIFF_THRESHOLD_BYTES


@contextlib.contextmanager
def ome_tiff_writer(path, *, bigtiff: bool = True, ome: bool = True):
    """An open ``tifffile.TiffWriter``, for a caller that must issue SEVERAL writes.

    ``write_ome_tiff`` below covers the single-shot case and is what most callers want.
    This exists for the two shapes it cannot express:

    * ``bin/merge_channels_pyramid.py`` writes the base resolution with ``subifds=N`` and
      then each pyramid level onto the SAME open writer -- several ``.write()`` calls that
      have to share one file handle.
    * ``bin/apply_basic_profiles.py`` and ``bin/tiled_stitch.py`` feed generators that
      COMPUTE each tile (an illumination correction, a warp) rather than slicing a source
      array, so there is no array for ``write_ome_tiff`` to walk.

    The caller keeps its generator and its per-write arguments; what moves here is the
    writer's construction, which is the part that was being re-decided per file.
    ``tests/test_ome_io_is_the_only_writer.py`` is what makes this the only route.
    """
    writer = tifffile.TiffWriter(str(path), bigtiff=bigtiff, ome=ome)
    try:
        yield writer
    finally:
        writer.close()


def _axis0_slabs(chunk_sizes: Tuple[int, ...]) -> Iterator[Tuple[int, int]]:
    """Turn a dask axis-0 chunk-size tuple into ``(start, stop)`` half-open spans."""
    start = 0
    for size in chunk_sizes:
        yield start, start + size
        start += size


def _iter_planes(image_data: Any, shape: Tuple[int, ...]) -> Iterator[np.ndarray]:
    """Yield the stack's 2-D ``(Y, X)`` planes in write order, ONE SOURCE CHUNK AT A TIME.

    ``shape[:-2]`` are the non-spatial axes (``C``, and ``Z``/``T`` when the caller has
    them); they are walked in C order, which is the page order tifffile expects for
    ``shape=``.

    THE ASSUMPTION THIS FUNCTION MAKES EXPLICIT: how much memory the write costs is the
    READER's decision, not this function's. It is bounded by ONE axis-0 chunk of whatever
    the reader handed back -- one plane when the reader chunks per plane (the good case),
    the whole stack when it returns a single chunk.

    So the planes are NOT fetched one index at a time. Doing that is a REGRESSION, not
    merely a missed optimisation: dask does not cache across independent ``compute()``
    calls, so on a single-chunk array ``image_data[i]`` decodes the WHOLE stack, once per
    plane -- same peak as an eager write, times N the work. Measured on an 8-plane
    single-chunk fixture: 8 whole-stack decodes.

    A plain ``.rechunk()`` does not fix that either (measured: still 8 decodes).
    Rechunking splits the graph DOWNSTREAM of the source read, and every plane's
    ``compute()`` still re-reads the source chunk. Iterating in chunk-sized slabs is what
    actually bounds it, and it bounds it at exactly one decode per chunk for every
    chunking --
    ``tests/test_convert_streaming_write.py::test_a_chunk_is_decoded_once_whatever_the_chunking``
    pins 1/2/8-plane chunks at 8/4/1 decodes.

    Array-likes with no ``chunks`` (a plain ndarray, or the NDPI/HDF5 readers' output)
    keep the plain per-index walk: there is no decode to amortise, and the caller may only
    support scalar-tuple indexing.
    """
    lead = shape[:-2]
    chunks = getattr(image_data, "chunks", None)

    if not lead or not chunks:
        for index in itertools.product(*(range(n) for n in lead)):
            yield np.asarray(image_data[index])
        return

    trailing = tuple(range(n) for n in lead[1:])
    for start, stop in _axis0_slabs(chunks[0]):
        slab = np.asarray(image_data[start:stop])
        for index in itertools.product(range(stop - start), *trailing):
            yield slab[index]


def _iter_tiles(
    image_data: Any, shape: Tuple[int, ...], tile: Tuple[int, int]
) -> Iterator[np.ndarray]:
    """Yield TILES, row-major within each plane -- what tifffile's tiled writer wants.

    tifffile's iterator mode is tile-wise, not plane-wise, whenever ``tile=`` is set:
    it walks ``numtiles`` items per page and raises ``ValueError('tile is too large')``
    the moment one is bigger than a single tile. Feeding it ``_iter_planes`` therefore
    only works while a whole plane happens to FIT IN ONE TILE, which is exactly the case
    a small fixture creates -- tifffile pads an undersized item without complaint. So the
    striped-vs-tiled write was once covered by a test that could not fail, and the first
    real slide (30552 x 32072) failed at CONVERT_IMAGE with "tile is too large".
    ``tests/test_convert_streaming_write.py`` and ``tests/test_ome_io.py`` both now write
    a plane LARGER than one tile.

    Partial edge tiles are yielded short and tifffile pads them, so the output is
    identical for shapes that are not tile multiples.

    Peak memory is unchanged: this only re-slices what ``_iter_planes`` already decoded,
    so the bound is still ONE SOURCE CHUNK, and all of that function's chunk reasoning
    applies here untouched.
    """
    th, tw = tile
    for plane in _iter_planes(image_data, shape):
        h, w = plane.shape[-2:]
        for y in range(0, h, th):
            for x in range(0, w, tw):
                yield plane[..., y : y + th, x : x + tw]


def _warn_if_the_reader_chunked_the_whole_stack_together(
    image_data: Any, shape: Tuple[int, ...]
) -> None:
    """Say so, loudly, when the lazy read cannot actually save any memory.

    ``_iter_planes`` bounds the write at one axis-0 chunk, so a single-chunk array is
    never WORSE than an eager write. But it is not better either: that one decode is the
    whole decompressed stack. The saving is the reader's to give, and whether a given
    bioio plugin gives it is not something this pipeline controls -- so an operator who
    sized CONVERT_IMAGE expecting a per-plane peak is told here rather than finding out
    from an OOM.
    """
    chunks = getattr(image_data, "chunks", None)
    if not chunks or len(shape) < 3 or shape[0] < 2 or len(chunks[0]) != 1:
        return

    itemsize = np.dtype(image_data.dtype).itemsize
    total_gb = int(np.prod(shape)) * itemsize / 1e9
    logger.warning(
        f"The reader returned all {shape[0]} planes as one dask chunk, so the streamed "
        f"write must decode them together: peak memory is the whole {total_gb:.2f} GB "
        "decompressed stack, not one plane. Size this task from the DECOMPRESSED slide."
    )


def _downsample_plane(plane: np.ndarray, factor: int) -> np.ndarray:
    """Strided decimation for a pyramid level. Strided, not interpolated, on purpose:
    a pyramid level is a NAVIGATION aid, and this function is also used on LABEL images
    where any interpolation invents label ids that never existed."""
    return plane[::factor, ::factor]


def write_ome_tiff(
    path,
    data: Any,
    *,
    channels: Optional[List[str]],
    pixel_size_um: Union[float, Tuple[Optional[float], Optional[float]], None],
    tile: int = CONVERT_TIFF_TILE,
    pyramid_levels: int = 1,
    bigtiff: Optional[bool] = None,
    axes: str = "CYX",
    pixel_size_z_um: Optional[float] = None,
) -> None:
    """Write ``data`` as a TILED OME-TIFF, streaming, never holding the whole slide.

    ``data`` is ``(C, Y, X)`` for the default ``axes='CYX'``; it may be a plain ndarray OR
    a LAZY array-like (anything with ``.shape``, ``.dtype`` and per-index/slab indexing --
    a dask array is the case this was built for). Reading lazily and then writing eagerly
    saves nothing: a whole-array ``tifffile.imwrite`` passes its argument through
    ``numpy.asarray``, so the slide would simply be materialised at the write instead of
    at the read. Both halves had to change together, and this is the second half.

    The streaming form hands ``TiffWriter.write`` an ITERATOR of tiles together with an
    explicit ``shape=`` and ``dtype=``, which it cannot infer from a generator.

    THE WRITE IS TILED, NOT STRIPED -- see ``CONVERT_TIFF_TILE`` for the measurement.
    Tiling changes the on-disk layout, not the pixels.

    ``photometric="minisblack"`` is the precondition that makes ONE PAGE EQUAL ONE
    CHANNEL, which every downstream per-page read in this pipeline depends on. Without it
    tifffile stores a ``(C, H, W)`` array as one page with C samples and both ``key=`` and
    ``pages[i]`` raise IndexError. ``tests/test_slide_io_seam.py`` pins it.

    ``pyramid_levels`` > 1 writes the extra levels as subIFDs of the base page -- the
    layout QuPath reads -- each a strided 2x decimation of its parent. Each level's shape
    is derived from the ACTUAL downsampled array (``planes[0].shape``), never recomputed
    by ceil-division arithmetic, so an odd, non-square plane decimates correctly.
    ``bin/merge_channels_pyramid.py`` does NOT use this path: it needs per-level
    compression arguments, per-level resolution tags and its own validators, and goes
    through ``ome_tiff_writer`` instead.

    ``bigtiff=None`` means BigTIFF iff the payload exceeds ``BIGTIFF_THRESHOLD_BYTES``;
    pass ``True``/``False`` to assert.
    """
    shape = tuple(int(n) for n in data.shape)
    if len(shape) < 2:
        raise ValueError(
            f"write_ome_tiff needs at least a 2-D array, got shape {shape}"
        )

    if channels is not None and "C" in axes:
        n_channels = shape[axes.index("C")]
        if len(channels) != n_channels:
            raise ValueError(
                f"write_ome_tiff: {len(channels)} channel name(s) {channels} for a stack "
                f"with {n_channels} channel(s) (axes={axes!r}, shape={shape}). The names "
                "are read positionally by every downstream consumer, so a mismatched "
                "header is silently wrong rather than merely incomplete."
            )

    dtype = np.dtype(data.dtype)
    nbytes = int(np.prod(shape)) * dtype.itemsize
    _warn_if_the_reader_chunked_the_whole_stack_together(data, shape)

    metadata = ome_metadata(
        channels, pixel_size_um, axes=axes, pixel_size_z_um=pixel_size_z_um
    )
    levels = max(1, int(pyramid_levels))

    with ome_tiff_writer(
        path, bigtiff=_wants_bigtiff(bigtiff, nbytes), ome=True
    ) as writer:
        writer.write(
            _iter_tiles(data, shape, (tile, tile)),
            shape=shape,
            dtype=dtype,
            metadata=metadata,
            photometric="minisblack",
            tile=(tile, tile),
            subifds=levels - 1 if levels > 1 else None,
        )

        # The reduced levels. Each is derived from the level ABOVE it, not from the
        # base, so the decimation cost is geometric rather than quadratic -- and it is
        # materialised one level at a time, which for a >=2x reduction is always
        # smaller than the base the caller already handed us. The base itself was
        # never materialised as a WHOLE array above (it was streamed tile by tile), so
        # this is the first point the full base is held in memory; it is unavoidable
        # once a reduced level must be computed from it.
        if levels > 1:
            lead = shape[:-2]
            current = np.stack(list(_iter_planes(data, shape))).reshape(shape)
            for _level in range(1, levels):
                planes = [
                    _downsample_plane(plane, 2)
                    for plane in current.reshape((-1,) + current.shape[-2:])
                ]
                current = np.stack(planes).reshape(lead + planes[0].shape)
                writer.write(
                    _iter_tiles(current, tuple(current.shape), (tile, tile)),
                    shape=tuple(current.shape),
                    dtype=dtype,
                    photometric="minisblack",
                    tile=(tile, tile),
                    subfiletype=1,  # REDUCEDIMAGE
                )


def write_tiff(path, data: Any, **kwargs) -> Path:
    """Write a NON-OME TIFF, forwarding every keyword argument to tifffile verbatim.

    DELIBERATELY THIN, AND HERE FOR ONE REASON: it makes the seam TOTAL. Eight call sites
    in ``bin/`` write something that is not an OME slide -- the segmentation label masks
    (three backends plus the ``add_cycle`` re-extraction), one single-channel plane per
    marker, the multi-site pseudo-FOV stack BASICPY fits on, and two ImageJ QC composites
    -- and each makes decisions this module has no opinion about (a mask is zlib+BigTIFF
    and tiled; an ImageJ composite is neither, and in fact CANNOT be BigTIFF, tifffile
    refuses the combination). Inventing defaults here would change all eight the day they
    were migrated.

    So it sets NOTHING. What it buys is that ``tests/test_ome_io_is_the_only_writer.py``
    can be a closed rule -- no allowlist, no per-file exception -- and that a new writer
    anywhere in ``bin/`` is a call into this module rather than a fresh, unreviewed set of
    TIFF decisions. Anything OME-shaped belongs in ``write_ome_tiff``, which does have
    opinions and states them.
    """
    tifffile.imwrite(str(path), data, **kwargs)
    return Path(path)
