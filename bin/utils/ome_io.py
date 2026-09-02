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

import importlib.util
import logging
from pathlib import Path

__all__ = [
    "READERS",
    "UnsupportedFormatError",
    "detect_reader",
    "require_reader",
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
