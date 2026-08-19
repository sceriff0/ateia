#!/usr/bin/env python3
"""Merge single-channel TIFF files into a pyramidal multi-channel OME-TIFF."""

from __future__ import annotations

import argparse
import colorsys
import gc
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import numpy as np
import tifffile

# Add path for utils
sys.path.insert(0, str(Path(__file__).parent / "utils"))
from logger import configure_logging, get_logger
from tiled_io import open_lazy
from validation import StreamingWrappedValues

# WHY THESE TWO IMPORTS ARE UNGUARDED, having replaced a `try: ... except ImportError:`
# fallback that stubbed `detect_wrapped_values` out to `return False, 0, 0.0`.
#
# `bin/utils/validation.py` IS importable in this script's container. It was checked
# rather than assumed: containers/merge/Dockerfile installs numpy (1.26.4), and
# validation.py imports nothing else but the standard library, so the only way the old
# fallback could fire is if bin/utils were missing entirely -- in which case the `logger`
# import two lines up would already have failed, unguarded. What the fallback actually
# bought was a silent path in which the wrapped-value check never ran and said nothing,
# on the pipeline's most expensive artifact. Every other bin/ script that reads from
# bin/utils (apply_basic_profiles.py, tiled_stitch.py, split_multichannel.py) imports it
# unconditionally; this one now matches them.
#
# `tiled_io.open_lazy` imports zarr internally, so the module-level `HAS_ZARR` probe that
# used to gate `_read_channel_file`'s zarr branch is gone with that function. The lazy read
# is no longer an optimisation this script can decline -- it is how the pyramid is built.

os.environ["NUMBA_DISABLE_JIT"] = "0"
os.environ["NUMBA_CACHE_DIR"] = tempfile.gettempdir() + "/numba_cache"
os.environ["NUMBA_DISABLE_CACHING"] = "1"

logger = get_logger(__name__)

__all__ = ["main"]


def log(msg):
    logger.info(msg)


MARKER_COLORS = {
    "DAPI": (0, 0, 255),
    "CD45": (0, 255, 0),
    "CD3": (255, 255, 0),
    "CD8": (255, 0, 255),
    "CD4": (0, 255, 255),
    "CD14": (255, 128, 0),
    "CD163": (255, 0, 128),
    "FOXP3": (128, 255, 0),
    "PANCK": (255, 0, 0),
    "VIMENTIN": (128, 0, 255),
    "SMA": (0, 128, 255),
    "GZMB": (255, 128, 128),
    "PD1": (128, 255, 128),
    "PDL1": (255, 200, 100),
    "L1CAM": (100, 200, 255),
    "PAX2": (200, 100, 255),
    "CD74": (255, 255, 128),
    "Segmentation": (255, 255, 255),
}


def rgb_to_ome_color(r: int, g: int, b: int, a: int = 255) -> int:
    """Convert RGBA to OME-XML signed 32-bit ARGB color."""
    value = (a << 24) | (r << 16) | (g << 8) | b
    if value >= 0x80000000:
        value -= 0x100000000
    return value


def generate_channel_color(name: str, index: int) -> Tuple[int, int, int]:
    """Generate a color for a channel based on name or index."""
    # Check predefined colors first (exact match, case-insensitive) — avoids
    # substring collisions like "CD4" matching "CD45", or "CD1" matching "CD14".
    name_upper = name.upper()
    for key, rgb in MARKER_COLORS.items():
        if key.upper() == name_upper:
            return rgb

    # Generate color using golden ratio
    h = (index * 0.618033988749895) % 1.0
    s = 0.7 + (index % 3) * 0.1
    v = 0.85 + (index % 2) * 0.1
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


#: The integer dtypes `downsample_image` routes through a float32 intermediate before
#: rounding back. Named once because `_downsample_plane_f32` and both branches of
#: `downsample_image` have to agree about the set.
_ROUNDED_DTYPES = (
    np.uint8,
    np.uint16,
    np.uint32,
    np.int8,
    np.int16,
    np.int32,
)


def _downsample_plane_f32(plane_f32: np.ndarray, factor: int, out_dtype) -> np.ndarray:
    """Downsample ONE already-float32 plane by `factor`, returning it in `out_dtype`.

    A WHOLE PLANE, never a band or a tile, and that is a correctness constraint rather
    than a convenience. `cv2.INTER_AREA`'s effective vertical ratio is `h / (h // factor)`,
    which equals `factor` only when `factor` divides `h`; when it does not, the resample
    support crosses whatever band boundary you pick and no decomposition into bands
    reproduces the whole-image answer. Measured on uint16 at factor 2, whole image against
    256-row bands: exact at h=1024 and h=1000, **max difference 29642 counts** at h=1023,
    and wrong again at 999 and 30001. An odd-dimensioned slide is ordinary. So this
    function is the reason the pyramid is derived per channel from whole planes while only
    the WRITE is tiled -- see `_level_tiles`.

    Taking float32 in, rather than the source plane, is what lets the caller build the
    float32 buffer band by band and never hold the storage-dtype plane beside it.

    This is exactly the arithmetic `downsample_image`'s integer branch performs; that
    function calls this one, so the streaming path and the whole-array path cannot drift.
    cv2 is imported here rather than at module scope for the same reason it is there: the
    only caller that tolerates its absence is `downsample_image`, whose own `import cv2`
    fails first and falls back to block averaging.
    """
    import cv2

    h, w = plane_f32.shape
    resized = cv2.resize(
        plane_f32, (w // factor, h // factor), interpolation=cv2.INTER_AREA
    )
    out_dtype = np.dtype(out_dtype)
    if out_dtype.type in _ROUNDED_DTYPES:
        return np.round(resized).astype(out_dtype)
    return resized.astype(out_dtype)


def downsample_image(image: np.ndarray, factor: int) -> np.ndarray:
    """Downsample a 2D or 3D (CYX) image by a given factor."""
    try:
        import cv2

        if image.ndim == 2:
            h, w = image.shape
            new_h, new_w = h // factor, w // factor
            # cv2.INTER_AREA is good for downsampling but may introduce small rounding errors
            # For integer types, use float intermediate then round
            if image.dtype in _ROUNDED_DTYPES:
                return _downsample_plane_f32(
                    image.astype(np.float32), factor, image.dtype
                )
            else:
                return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
        elif image.ndim == 3:
            # CYX format - downsample each channel
            c, h, w = image.shape
            new_h, new_w = h // factor, w // factor
            result = np.zeros((c, new_h, new_w), dtype=image.dtype)
            for i in range(c):
                if image.dtype in _ROUNDED_DTYPES:
                    result[i] = _downsample_plane_f32(
                        image[i].astype(np.float32), factor, image.dtype
                    )
                else:
                    result[i] = cv2.resize(
                        image[i], (new_w, new_h), interpolation=cv2.INTER_AREA
                    )
            return result
    except ImportError:
        pass

    # Fallback: block averaging
    if image.ndim == 2:
        h, w = image.shape
        new_h, new_w = h // factor, w // factor
        trimmed = image[: new_h * factor, : new_w * factor]
        reshaped = trimmed.reshape(new_h, factor, new_w, factor)
        if image.dtype in [np.float32, np.float64]:
            return reshaped.mean(axis=(1, 3)).astype(image.dtype)
        else:
            # Fix: Round before converting to integer dtype to avoid truncation artifacts
            return np.round(reshaped.mean(axis=(1, 3))).astype(image.dtype)
    elif image.ndim == 3:
        c, h, w = image.shape
        new_h, new_w = h // factor, w // factor
        result = np.zeros((c, new_h, new_w), dtype=image.dtype)
        for i in range(c):
            trimmed = image[i, : new_h * factor, : new_w * factor]
            reshaped = trimmed.reshape(new_h, factor, new_w, factor)
            # Fix: Round before converting to integer dtype to avoid truncation artifacts
            if image.dtype in [np.float32, np.float64]:
                result[i] = reshaped.mean(axis=(1, 3)).astype(image.dtype)
            else:
                result[i] = np.round(reshaped.mean(axis=(1, 3))).astype(image.dtype)
        return result
    else:
        raise ValueError(
            f"downsample_image only supports 2D or 3D (CYX) images, got ndim={image.ndim}"
        )


def calculate_pyramid_levels(
    height: int,
    width: int,
    min_size: int = 64,
    max_levels: int = 10,
    scale_factor: int = 2,
) -> List[Tuple[int, int]]:
    """Calculate pyramid level dimensions."""
    levels = [(height, width)]
    h, w = height, width

    for _ in range(max_levels - 1):
        h = h // scale_factor
        w = w // scale_factor
        if h < min_size or w < min_size:
            break
        levels.append((h, w))

    return levels


def build_ome_xml(
    width: int,
    height: int,
    num_channels: int,
    dtype: np.dtype,
    channel_names: List[str],
    channel_colors: List[Tuple[int, int, int]],
    physical_size_x: float = 0.325,
    physical_size_y: float = 0.325,
    physical_size_unit: str = "µm",
) -> str:
    """
    Build OME-XML metadata for multi-channel pyramidal image.

    IMPORTANT: For tifffile's automatic OME-TIFF handling, we use the
    metadata dict approach rather than raw XML for the base image,
    but we can still embed additional annotations.
    """
    dtype_map = {
        "uint8": "uint8",
        "uint16": "uint16",
        "uint32": "uint32",
        "int8": "int8",
        "int16": "int16",
        "int32": "int32",
        "float32": "float",
        "float64": "double",
    }
    ome_dtype = dtype_map.get(str(dtype), "uint16")

    # Build channel elements
    channel_elements = []
    for i, (name, color) in enumerate(zip(channel_names, channel_colors)):
        ome_color = rgb_to_ome_color(*color)
        safe_name = xml_escape(name)
        channel_elements.append(
            f'      <Channel ID="Channel:0:{i}" Name="{safe_name}" '
            f'Color="{ome_color}" SamplesPerPixel="1"/>'
        )

    channels_xml = "\n".join(channel_elements)

    # Build complete OME-XML
    # NOTE: TiffData is left simple - tifffile will handle IFD mapping
    ome_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xsi:schemaLocation="http://www.openmicroscopy.org/Schemas/OME/2016-06 http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd">
  <Image ID="Image:0" Name="MultiplexImage">
    <Pixels ID="Pixels:0"
            Type="{ome_dtype}"
            SizeX="{width}"
            SizeY="{height}"
            SizeZ="1"
            SizeC="{num_channels}"
            SizeT="1"
            DimensionOrder="XYCZT"
            PhysicalSizeX="{physical_size_x}"
            PhysicalSizeY="{physical_size_y}"
            PhysicalSizeXUnit="{physical_size_unit}"
            PhysicalSizeYUnit="{physical_size_unit}"
            Interleaved="false"
            BigEndian="false">
{channels_xml}
      <TiffData/>
    </Pixels>
  </Image>
</OME>'''

    return ome_xml


def to_uint16(data):
    """The one float->uint16 rule for this pipeline: clip to range, ROUND, then cast.

    `.astype()` truncates toward zero, so casting without rounding applies a systematic
    downward bias of up to one count to every non-integral pixel -- mean -0.5 LSB, one-sided,
    on intensity data that later becomes a measured per-cell statistic. It never averages out.

    Rounding is `np.round` (half-to-even) to match the convention
    `bin/apply_basic_profiles.py`'s `_to_storage_dtype` states and explains (it inherited the
    rule, verbatim, from the deleted `bin/preprocess.py`, which stated it first); a different
    tie rule here would be a second convention, not a fix. Integer input is returned untouched
    so this is safe to call unconditionally.

    Clipping stays ahead of the round for a reason: 65535.6 rounds to 65536, which wraps to 0
    in uint16. Guarded by tests/test_dtype_rounding_contract.py.
    """
    if not np.issubdtype(data.dtype, np.floating):
        return data
    return np.round(np.clip(data, 0, 65535)).astype(np.uint16)


#: Byte budget for one streamed source row band. Peak for the base pass is one band plus
#: one write tile, and expressing the budget in BYTES rather than in rows is what keeps
#: that independent of slide width -- a fixed row count on a 40000px-wide slide is ten
#: times the same band on a 4000px one. `bin/utils/tiled_io.py` sizes its decimated reads
#: the same way and for the same reason.
#:
#: This script owns the constant. It is deliberately NOT imported from convert_image.py or
#: split_multichannel.py and NOT hoisted into bin/utils/: those scripts run in different
#: container images, and a cross-script import would couple their dependency sets. Same
#: reason `to_uint16` is written out here rather than imported.
#:
#: 64 MiB is `tiled_io.DEFAULT_BAND_BYTES`' value, arrived at independently and kept the
#: same: a band only has to hold enough rows to cut one row of write tiles out of, and at
#: the shipped 512px tile on a 40000px-wide slide one tile row is 41 MB. A larger budget
#: would buy nothing and would sit in the memory formula as a constant adder.
READ_BAND_BYTES = 64 * 1024 * 1024


def _band_rows(width: int, itemsize: int, align: int = 1) -> int:
    """Rows per streamed band: the most that fit in READ_BAND_BYTES, snapped DOWN to a
    multiple of `align` and never below it.

    `align` is the write-tile size on the base pass, so a band always contains whole tile
    rows and no tile is ever assembled from two reads. It is 1 when the band is only
    filling a float32 buffer, where nothing depends on the boundary.
    """
    align = max(1, int(align))
    per_row = max(1, int(width) * max(1, int(itemsize)))
    rows = max(align, int(READ_BAND_BYTES) // per_row)
    return max(align, (rows // align) * align)


class _ChannelValidator:
    """The per-channel checkpoint `_load_channel_stack` ran on a resident plane, folded
    over row bands and emitting THE SAME lines, once, after the channel's last band.

    Deliberately NOT `validation.StreamingNegativeClip`, even though the shape is identical.
    That class reproduces `clip_negative_values`'/`detect_negative_values`' log lines; this
    script's are different ones -- four-space-indented, naming the channel file, and with
    the min rather than a count. Reusing it would keep the pixels and change the log, which
    is the one thing this rewrite is not allowed to do. The wrapped-value half DOES come
    from validation.py (`StreamingWrappedValues`), because there the aggregate, the
    threshold and the emitted line are all the shared function's.

    The clip itself is applied to every band unconditionally by the source, not only when
    the whole channel turns out to have negatives: `np.clip(x, 0, None)` is a no-op on
    already-non-negative data, so the pixels are identical either way, while the DECISION
    to log cannot be taken until the last band has been seen.
    """

    def __init__(self, name: str, dtype):
        self.name = name
        dt = np.dtype(dtype)
        self._min_before = None
        self._min_after = None
        # `_load_channel_stack`'s branch was `elif dtype == np.uint16`, on the dtype scanned
        # from the file -- so the wrapped-value check ran on uint16 channels only, and only
        # when the channel had no negatives at all.
        self._wrapped = StreamingWrappedValues(dt) if dt == np.uint16 else None

    def observe(self, raw: np.ndarray, clipped: np.ndarray) -> None:
        """Fold one band's raw (pre-clip) and clipped values into the aggregates."""
        if raw.size == 0:
            return
        min_before = raw.min()
        if self._min_before is None or min_before < self._min_before:
            self._min_before = min_before
        min_after = clipped.min()
        if self._min_after is None or min_after < self._min_after:
            self._min_after = min_after
        if self._wrapped is not None:
            self._wrapped.process(raw)

    def finalize(self) -> None:
        """Emit the channel's aggregate line(s) -- the same ones, with the same numbers."""
        if self._min_before is None:
            return
        if self._min_before < 0:
            log(
                f"    WARNING: Negative values detected in {self.name}: min={self._min_before}"
            )
            log(f"    Clipped to 0. New min={self._min_after}")
        elif self._wrapped is not None:
            # Percentages against the WHOLE channel, not against a band: the count and the
            # size are summed across bands and divided once, inside finalize().
            has_wrapped, wrap_count, wrap_pct = self._wrapped.finalize()
            if has_wrapped:
                log(
                    f"    WARNING: {wrap_count} potential wrapped negative pixels ({wrap_pct:.4f}%) in {self.name}"
                )


class _BandSource:
    """A (C, H, W) pixel source that hands out row bands already in the STORAGE dtype.

    Subclasses supply `read_band`; everything the pyramid needs is built from it. The point
    of the seam is that `write_pyramidal_ome_tiff_from_source` never learns whether the
    pixels came from files or from a resident array, so the in-memory API kept for callers
    and tests and the gigapixel file path share one writer.
    """

    #: (C, H, W)
    shape: Tuple[int, int, int] = (0, 0, 0)
    #: dtype the pyramid is written in -- what `read_band` returns.
    dtype = np.dtype(np.uint16)
    #: itemsize of the SOURCE pixels, which is what a band read actually materialises.
    #: Distinct from `dtype.itemsize` because a float32 source is stored as uint16: budgeting
    #: the band by the output's 2 bytes/px would let a float channel's read be twice the
    #: intended size before the cast even happens. (The float path is still under-counted --
    #: `read_band` transiently holds the raw float32, a clipped float32 copy and the uint16
    #: result, ~10 bytes/px against a 4 byte/px budget -- but under-counting by 2.5x beats
    #: under-counting by 5x, and the band is a constant in the memory formula either way.)
    src_itemsize = 2

    def read_band(self, c: int, y0: int, y1: int, validator=None) -> np.ndarray:
        raise NotImplementedError

    def release(self) -> None:
        """Drop any open handle. Safe to call at any time; `read_band` reopens."""

    def plane_float32(self, c: int) -> np.ndarray:
        """Channel `c` as ONE float32 plane, assembled band by band.

        Never materialises the storage-dtype plane beside the float32 one: each band is
        cast on assignment into the preallocated buffer, which is elementwise-identical to
        `plane.astype(np.float32)` and costs 4 bytes per pixel instead of 6.
        """
        _, h, w = self.shape
        buf = np.empty((h, w), dtype=np.float32)
        band = _band_rows(w, self.src_itemsize)
        for y0 in range(0, h, band):
            y1 = min(y0 + band, h)
            buf[y0:y1] = self.read_band(c, y0, y1)
        self.release()
        return buf

    def level_chain(self, c: int, scale: int, n_levels: int):
        """Every pyramid level of channel `c`, as ``[level 1, level 2, ...]``.

        Iterated halving, not one cumulative resize: the whole-array implementation fed
        each level's output back in as the next level's input, rounding to the storage
        dtype in between (`current_data = downsampled`), and a single resize by
        ``scale**k`` is a different computation from k resizes by ``scale`` with a round
        between each. It would also not be pixel-identical, which is the constraint.

        The base plane is transient: it exists as one float32 buffer for the duration of
        the first resize and is dropped. What the caller keeps is the chain, which is
        about a third of ONE plane -- not of the stack.
        """
        planes = [_downsample_plane_f32(self.plane_float32(c), scale, self.dtype)]
        for _ in range(n_levels - 1):
            planes.append(
                _downsample_plane_f32(planes[-1].astype(np.float32), scale, self.dtype)
            )
        return planes


class _ArraySource(_BandSource):
    """A resident (C, H, W) array behind the band interface.

    The defensive float->uint16 cast the whole-array writer used to do up front, over the
    entire stack, now happens one band at a time. `to_uint16` is elementwise, so the pixels
    are identical.

    IT DOES NOT CLIP NEGATIVES, and that asymmetry with `_ChannelFileSource` is deliberate.
    The negative clip belongs to `_load_channel_stack` -- the PIPELINE's read of the channel
    files -- and never ran in `write_pyramidal_ome_tiff`, which only ever cast float input.
    Clipping here would silently change what this entry point does with signed-integer data:
    an int16 stack with a -100 pixel used to round-trip -100, and a clip turns it into 0. A
    first draft of this class did exactly that, uncovered.
    `tests/test_merge_pyramid_streaming.py::test_the_array_entry_point_does_not_clip_signed_input`
    pins the round-trip. Float input is still clipped, by `to_uint16`, exactly as before.
    """

    def __init__(self, data: np.ndarray):
        self._data = data
        src_dtype = np.dtype(data.dtype)
        self._need_cast = src_dtype in (np.dtype(np.float32), np.dtype(np.float64))
        self.dtype = np.dtype(np.uint16) if self._need_cast else src_dtype
        self.src_itemsize = src_dtype.itemsize
        self.shape = tuple(int(n) for n in data.shape)
        if self._need_cast:
            log("  Casting float data to uint16 for QuPath compatibility (per-band)")

    def read_band(self, c: int, y0: int, y1: int, validator=None) -> np.ndarray:
        band = self._data[c, y0:y1]
        if validator is not None:
            validator.observe(band, band)
        return to_uint16(band) if self._need_cast else band


class _ChannelFileSource(_BandSource):
    """The single-channel TIFFs SPLIT_CHANNELS wrote, read one row band at a time.

    Replaces `_read_channel_file`, which opened a tifffile zarr store and then immediately
    called `np.asarray` on the whole of it -- so the open bought nothing and the docstring's
    "via zarr when available" described a laziness that was not there. `open_lazy` is the
    repository's one region-readable view (bin/utils/tiled_io.py) and is what
    tiled_stitch.py and apply_basic_profiles.py read through. Region reads against these
    files are genuinely cheap now that SPLIT_CHANNELS writes them tiled.

    One handle is kept open across the bands of a single channel and closed when another
    channel is selected: `open_lazy` rebuilds the store on every call by design, so reopening
    per band would pay for the file's directory on every read.
    """

    def __init__(self, channel_files, height: int, width: int, dtype):
        self.channel_files = [str(p) for p in channel_files]
        self.shape = (len(self.channel_files), int(height), int(width))
        src_dtype = np.dtype(dtype)
        # uint16 when the input is float, so nothing downstream pays for a bulk cast --
        # the same choice `_load_channel_stack` made when it allocated the output stack.
        self._need_cast = src_dtype in (np.dtype(np.float32), np.dtype(np.float64))
        self._signed = not np.issubdtype(src_dtype, np.unsignedinteger)
        self.dtype = np.dtype(np.uint16) if self._need_cast else src_dtype
        self.src_itemsize = src_dtype.itemsize
        self._open_index = None
        self._arr = None
        self._close = None

    def _select(self, c: int) -> None:
        if self._open_index == c:
            return
        self.release()
        arr, _dtype, close = open_lazy(self.channel_files[c])
        if arr.shape[0] != 1:
            close()
            raise ValueError(
                f"{self.channel_files[c]}: expected a single-plane channel file, got "
                f"{arr.shape[0]} planes. SPLIT_CHANNELS writes one plane per marker."
            )
        self._arr, self._close, self._open_index = arr, close, c

    def release(self) -> None:
        if self._close is not None:
            self._close()
        self._arr = None
        self._close = None
        self._open_index = None

    def read_band(self, c: int, y0: int, y1: int, validator=None) -> np.ndarray:
        self._select(c)
        width = self.shape[2]
        raw = np.asarray(self._arr[0, slice(y0, y1), slice(0, width)])
        band = np.clip(raw, 0, None) if self._signed else raw
        if validator is not None:
            validator.observe(raw, band)
        return to_uint16(band) if self._need_cast else band


class _ArrayMaskSource(_BandSource):
    """A resident (2, H, W) mask stack behind the band interface."""

    dtype = np.dtype(np.uint32)
    src_itemsize = 4

    def __init__(self, mask_stack: np.ndarray):
        self._data = mask_stack
        self.shape = tuple(int(n) for n in mask_stack.shape)

    def read_band(self, c: int, y0: int, y1: int, validator=None) -> np.ndarray:
        return self._data[c, y0:y1].astype(np.uint32)


class _MaskFileSource(_BandSource):
    """The cell and nuclei mask TIFFs, read one row band at a time.

    Streaming the mask series is worth doing even though it carries no pyramid: it is a
    full slide-sized uint32 stack, 8 bytes per pixel against the intensity series' 2, so on
    a 40000x30000 slide the resident version was 9.6 GB -- larger than everything else this
    process holds put together. Nothing about the written bytes changes, because
    `astype(np.uint32)` is elementwise and the write is tiled either way. No pyramid is
    built here for the reason the whole-array version already documented: mean-downsampling
    would corrupt categorical label IDs.
    """

    dtype = np.dtype(np.uint32)
    src_itemsize = 4

    def __init__(self, cell_path: str, nuclei_path: str, height: int, width: int):
        self._paths = [str(cell_path), str(nuclei_path)]
        self.shape = (2, int(height), int(width))
        self._open_index = None
        self._arr = None
        self._close = None

    def _select(self, c: int) -> None:
        if self._open_index == c:
            return
        self.release()
        arr, _dtype, close = open_lazy(self._paths[c])
        # The whole-array version squeezed the mask and then compared its shape against the
        # intensity plane's, so a multi-plane mask file failed loudly. `_open_mask_source`
        # only sees the first page's Y/X, so the plane count is checked here instead --
        # without this, plane 0 would be embedded silently.
        if arr.shape[0] != 1:
            close()
            raise ValueError(
                f"{self._paths[c]}: expected a single-plane mask, got {arr.shape[0]} "
                "planes. SEGMENT writes one plane per mask."
            )
        self._arr, self._close, self._open_index = arr, close, c

    def release(self) -> None:
        if self._close is not None:
            self._close()
        self._arr = None
        self._close = None
        self._open_index = None

    def read_band(self, c: int, y0: int, y1: int, validator=None) -> np.ndarray:
        self._select(c)
        width = self.shape[2]
        return np.asarray(self._arr[0, slice(y0, y1), slice(0, width)]).astype(
            np.uint32
        )


def _plane_tiles(source: _BandSource, tile: int, validators=None):
    """Yield a (C, H, W) source as full-resolution tiles in tifffile's order.

    tifffile's order is (channel, tile row, tile column) and every tile is handed over at
    the full `tile` x `tile` size with the slide's edge zero-padded, exactly as
    bin/apply_basic_profiles.py and bin/tiled_stitch.py do. A FRESH buffer per tile is not
    an oversight: the writer consumes the generator lazily, so a reused buffer would be
    overwritten before it had been encoded.

    Bands are read at whole-tile-row granularity so a tile is never assembled from two
    reads, and each band is dropped before the next is fetched -- peak here is one band
    plus one tile, independent of the channel count and of the slide's height.
    """
    c_n, h, w = source.shape
    band_rows = _band_rows(w, source.src_itemsize, tile)
    for c in range(c_n):
        validator = validators[c] if validators else None
        if validator is not None:
            log(f"  Streaming: {validator.name}")
        for band_y0 in range(0, h, band_rows):
            band_y1 = min(band_y0 + band_rows, h)
            band = source.read_band(c, band_y0, band_y1, validator=validator)
            for y0 in range(band_y0, band_y1, tile):
                th = min(tile, band_y1 - y0)
                for x0 in range(0, w, tile):
                    tw = min(tile, w - x0)
                    out = np.zeros((tile, tile), dtype=source.dtype)
                    out[:th, :tw] = band[y0 - band_y0 : y0 - band_y0 + th, x0 : x0 + tw]
                    yield out
            del band
        # NOTHING that must happen may go after the last `yield`: tifffile stops calling
        # next() as soon as it has taken the final tile it needs, so a generator's tail
        # never runs for the LAST channel. `validator.finalize()` lived here at first and
        # a one-channel run printed no validation line at all -- silently, because the
        # pixels were fine. The validators are finalized by the caller, after `write`
        # returns; `source.release()` is likewise re-run by `merge_channels`' finally.
        source.release()
        gc.collect()


class _PyramidLevels:
    """Derives every pyramid level once, per channel, and serves them in tifffile's order.

    THE ORDERING CONSTRAINT, WHICH IS WHAT MAKES THIS A CLASS AND NOT A LOOP. tifffile
    fills the reserved SubIFDs one level at a time: all channels of level 1, then all
    channels of level 2, and so on. Deriving level k from level k-1 -- which is what the
    whole-array version did, and what pixel-identity requires -- therefore means level k-1
    must still exist when level k is built, across a channel loop that has run to the end
    in between.

    WHAT IS HELD, AND WHY IT IS THIS AND NOT SOMETHING CHEAPER OR DEARER. Level 1's pass
    reads each channel's base plane once and derives that channel's WHOLE chain
    (`level_chain`), keeps levels 2..K, and hands level 1 straight to the writer. So:

      * level 1 itself is never accumulated across channels -- it is the biggest level, a
        quarter of the base, and it is transient here;
      * what IS accumulated is levels 2 and deeper, which sum to `H*W/6` bytes per channel,
        one twelfth of a base plane. On an 8-channel 40000x30000 uint16 slide that is
        1.6 GB against the 19 GB stack the old code allocated before writing anything;
      * each level is dropped (`drop`) as soon as it has been written, so the peak is the
        moment level 1's pass ends, not the end of the run.

    Two designs were measured and rejected. Accumulating EVERY level (the obvious form)
    holds `base/3` -- 6.4 GB on that slide, four times this. Holding nothing at all and
    re-deriving level k from a fresh read of the base makes the peak genuinely independent
    of the channel count, but re-reads every channel's source file once per level: measured
    on a 4-channel 8193x8193 slide, 28 whole-plane decodes instead of 4, and 18.4 s against
    5.9 s for the whole-array version -- a 3.4x slowdown on a process that already has 6.5 h
    tasks in production. This form reads each source plane twice (once for the base, once
    here) and costs 1.4x.
    """

    def __init__(self, source: _BandSource, scale: int, n_sublevels: int):
        self._source = source
        self._scale = scale
        self._n = n_sublevels
        #: (channel, level) -> plane, for levels 2..K only.
        self._stash: Dict[Tuple[int, int], np.ndarray] = {}

    def tiles(self, level_index: int, level_shape, tile: int):
        """Yield level `level_index` as tiles, in tifffile's (channel, row, col) order."""
        c_n = self._source.shape[0]
        lh, lw = level_shape
        for c in range(c_n):
            if level_index == 1:
                chain = self._source.level_chain(c, self._scale, self._n)
                plane = chain[0]
                for offset, deeper in enumerate(chain[1:], start=2):
                    self._stash[(c, offset)] = deeper
                del chain
            else:
                plane = self._stash[(c, level_index)]
            for y0 in range(0, lh, tile):
                th = min(tile, lh - y0)
                for x0 in range(0, lw, tile):
                    tw = min(tile, lw - x0)
                    out = np.zeros((tile, tile), dtype=self._source.dtype)
                    out[:th, :tw] = plane[y0 : y0 + th, x0 : x0 + tw]
                    yield out
            del plane
            # Nothing load-bearing after the last `yield` -- see the note in `_plane_tiles`.
            self._source.release()
            gc.collect()

    def drop(self, level_index: int) -> None:
        """Release a level once it is on disk. Called by the writer, not by `tiles`, because
        a generator's tail does not run for the last channel."""
        for key in [k for k in self._stash if k[1] == level_index]:
            del self._stash[key]
        gc.collect()


def write_pyramidal_ome_tiff_from_source(
    source: _BandSource,
    output_path: str,
    channel_names: List[str],
    channel_colors: List[Tuple[int, int, int]],
    physical_size_x: float = 0.325,
    physical_size_y: float = 0.325,
    pyramid_resolutions: int = 5,
    pyramid_scale: int = 2,
    tile_size: int = 512,
    compression: str = "zstd",
    compressionargs: Optional[Dict] = None,
    mask_source: Optional[_BandSource] = None,
    mask_names: Optional[List[str]] = None,
    validators=None,
):
    """Write the pyramidal OME-TIFF without ever holding the slide.

    The IFD structure Bio-Formats/QuPath expects is unchanged -- base resolution as one
    page per channel, pyramid levels in the reserved SubIFDs, an optional second OME image
    for the masks. What changed is that every one of those writes is fed from a generator
    of tiles instead of from a resident array:

      * the BASE comes straight from the source's row bands, so no (C, H, W) array exists
        at any point;
      * each LEVEL is derived per channel from a whole plane (mandatory -- see
        `_downsample_plane_f32`) and tiled only on the way out;
      * the MASK series streams the two mask files the same way.

    Peak is therefore one float32 plane plus that plane's level chain -- constant in slide
    size, but NOT constant in channel count: levels 2..K of each channel's chain are
    stashed until they are written (`_PyramidLevels`, below), which sums to `H*W/6` bytes
    per channel, one twelfth of a base plane. Measured slope: 0.69 MB/channel, down from
    10.1 MB/channel before this rewrite -- a deliberate trade against the 3.4x wall-clock
    cost of re-deriving every level from a fresh read instead (see `_PyramidLevels`' own
    docstring for the two rejected alternatives and their numbers). Verified byte-for-byte
    against the whole-array writer: same file size, same tags, same OME-XML but for
    tifffile's random per-file UUID.

    Parameters are the same as `write_pyramidal_ome_tiff`, except that `source` and
    `mask_source` replace the `data` and `mask_stack` arrays, and `validators` carries the
    optional per-channel `_ChannelValidator` objects folded during the base pass.
    """
    num_channels, height, width = source.shape

    # Calculate pyramid levels
    levels = calculate_pyramid_levels(
        height,
        width,
        min_size=64,
        max_levels=pyramid_resolutions,
        scale_factor=pyramid_scale,
    )
    num_subresolutions = len(levels) - 1

    log(f"Writing pyramidal OME-TIFF with {len(levels)} resolution levels:")
    for i, (h, w) in enumerate(levels):
        log(f"  Level {i}: {w} x {h}")

    # Build metadata dict for tifffile (it will generate proper OME-XML)
    metadata = {
        "axes": "CYX",
        "Channel": {"Name": channel_names},
        "PhysicalSizeX": physical_size_x,
        "PhysicalSizeXUnit": "µm",
        "PhysicalSizeY": physical_size_y,
        "PhysicalSizeYUnit": "µm",
    }

    # Common write options
    options = dict(
        tile=(tile_size, tile_size),
        compression=compression,
        photometric="minisblack",
        resolutionunit="CENTIMETER",
        # ONE COMPRESSOR THREAD, PINNED. tifffile defaults `maxworkers` to the machine's
        # CPU count for a compressed tiled write and dispatches encoded tiles through a
        # thread pool, so the pool runs ahead of the writer and the queued tiles become an
        # unbounded term in the peak -- one that scales with the CHANNEL COUNT, which is
        # exactly the property this rewrite exists to remove. Measured end to end on a
        # 1024^2 slide with 4 pyramid levels, peak in units of one plane:
        #
        #                       C=2    C=8    C=12   C=16
        #   tifffile 2023.4.12  8.24   4.78   5.10   5.41   (default maxworkers)
        #   tifffile 2025.5.10  7.44   9.41  13.54  17.54   (default maxworkers)
        #
        # The container pins 2025.5.10 (containers/merge/Dockerfile), where the slope is
        # about ONE PLANE PER CHANNEL. `maxworkers=1` removes it at both versions.
        #
        # It costs nothing worth having. This process reserves `cpus = 2`, and tifffile
        # reads `os.cpu_count()` rather than the cgroup, so on a 64-core node it was
        # spinning up 64 compressor threads against a 2-CPU reservation. Measured on a
        # 4-channel 8193^2 slide at 2025.5.10: default 661.5 MB / 5.9 s, maxworkers=2
        # 641.6 MB / 5.6 s, maxworkers=1 477.1 MB / 6.3 s -- the wall clock is within noise
        # and only 1 is deterministic. Written bytes are unaffected; the byte-for-byte
        # identity test in tests/test_merge_pyramid_streaming.py passes at both versions.
        maxworkers=1,
    )
    if compressionargs:
        options["compressionargs"] = compressionargs

    with tifffile.TiffWriter(output_path, bigtiff=True, ome=True) as tif:
        # Write base resolution with all channels
        # subifds parameter reserves space for pyramid levels
        log(f"  Writing base resolution ({width} x {height})...")
        tif.write(
            _plane_tiles(source, tile_size, validators=validators),
            shape=(num_channels, height, width),
            dtype=source.dtype,
            subifds=num_subresolutions,
            resolution=(1e4 / physical_size_x, 1e4 / physical_size_y),
            metadata=metadata,
            **options,
        )

        # The per-channel validation aggregates, emitted here rather than from the tail of
        # `_plane_tiles`' per-channel loop. tifffile stops pulling from the generator once
        # it has the last tile it needs, so anything after the final `yield` never runs --
        # measured, a one-channel run emitted no validation line at all while writing a
        # perfectly correct pyramid. One line per channel, in channel order, still.
        for validator in validators or ():
            validator.finalize()

        # Generate and write pyramid levels
        pyramid = _PyramidLevels(source, pyramid_scale, num_subresolutions)
        for level_idx in range(1, len(levels)):
            level_h, level_w = levels[level_idx]
            log(f"  Writing pyramid level {level_idx} ({level_w} x {level_h})...")

            tif.write(
                pyramid.tiles(level_idx, levels[level_idx], tile_size),
                shape=(num_channels, level_h, level_w),
                dtype=source.dtype,
                subfiletype=1,  # REDUCEDIMAGE flag for pyramid level
                resolution=(
                    1e4 / (physical_size_x * (pyramid_scale**level_idx)),
                    1e4 / (physical_size_y * (pyramid_scale**level_idx)),
                ),
                **options,
            )
            # Freed here rather than in the generator, whose tail does not run for the
            # last channel; holding a written level would make the peak the SUM of the
            # levels instead of the deepest live pair.
            pyramid.drop(level_idx)

        # Optional second series: segmentation masks (cell + nuclei), uint32,
        # single full-resolution. This is a fresh top-level tif.write (no
        # subifds/subfiletype) which tifffile records as a NEW OME image
        # (Image:1) rather than a pyramid level of Image:0. Mean-downsampling
        # would corrupt categorical label IDs, so no pyramid is built here.
        if mask_source is not None:
            log(f"  Writing mask series (Image:1): {mask_source.shape} uint32")
            tif.write(
                _plane_tiles(mask_source, tile_size),
                shape=mask_source.shape,
                dtype=mask_source.dtype,
                metadata={
                    "axes": "CYX",
                    "Channel": {"Name": mask_names or ["cell_mask", "nuclei_mask"]},
                },
                tile=(tile_size, tile_size),
                compression=compression,
                photometric="minisblack",
                resolutionunit="CENTIMETER",
                maxworkers=1,  # see `options` above
            )

    log(f"Pyramidal OME-TIFF complete: {output_path}")

    # Verify the output
    verify_ome_tiff(output_path)


def write_pyramidal_ome_tiff(
    data: np.ndarray,  # Full CYX array
    output_path: str,
    channel_names: List[str],
    channel_colors: List[Tuple[int, int, int]],
    physical_size_x: float = 0.325,
    physical_size_y: float = 0.325,
    pyramid_resolutions: int = 5,
    pyramid_scale: int = 2,
    tile_size: int = 512,
    compression: str = "zstd",
    compressionargs: Optional[Dict] = None,
    mask_stack: Optional[np.ndarray] = None,
    mask_names: Optional[List[str]] = None,
):
    """
    Write a pyramidal OME-TIFF with proper QuPath-compatible structure, from arrays.

    The whole-array entry point, kept because callers and tests that already hold a small
    stack should not have to build a source object. It wraps the arrays in `_ArraySource` /
    `_ArrayMaskSource` and defers to `write_pyramidal_ome_tiff_from_source`, which is where
    the structure and the ordering are decided; the pipeline itself goes through the file
    source and never materialises `data`.

    Parameters
    ----------
    data : ndarray
        3D numpy array in CYX order (channels, height, width).
    output_path : str
        Output file path.
    channel_names : list of str
        List of channel names.
    channel_colors : list of tuple
        List of (R, G, B) tuples.
    physical_size_x : float
        Pixel size in X (micrometers).
    physical_size_y : float
        Pixel size in Y (micrometers).
    pyramid_resolutions : int
        Number of pyramid levels.
    pyramid_scale : int
        Downscaling factor between levels.
    tile_size : int
        Tile size for efficient access.
    compression : str
        Compression algorithm.
    mask_stack : ndarray, optional
        Optional (2, H, W) uint32 array of segmentation masks (cell + nuclei
        labels). Written as a SECOND, single-resolution OME series (Image:1)
        so categorical label IDs are never mean-downsampled. Does not affect
        the intensity series (Image:0).
    mask_names : list of str, optional
        Channel names for the mask series (default ['cell_mask', 'nuclei_mask']).
    """
    write_pyramidal_ome_tiff_from_source(
        _ArraySource(data),
        output_path,
        channel_names,
        channel_colors,
        physical_size_x=physical_size_x,
        physical_size_y=physical_size_y,
        pyramid_resolutions=pyramid_resolutions,
        pyramid_scale=pyramid_scale,
        tile_size=tile_size,
        compression=compression,
        compressionargs=compressionargs,
        mask_source=None if mask_stack is None else _ArrayMaskSource(mask_stack),
        mask_names=mask_names,
    )


def verify_ome_tiff(path: str):
    """Verify the OME-TIFF structure is correct."""
    log("Verifying OME-TIFF structure...")
    with tifffile.TiffFile(path) as tif:
        log(f"  Number of pages: {len(tif.pages)}")
        log(f"  Number of series: {len(tif.series)}")

        if tif.series:
            series = tif.series[0]
            log(f"  Series 0 shape: {series.shape}")
            log(f"  Series 0 axes: {series.axes}")
            log(f"  Series 0 is_pyramidal: {series.is_pyramidal}")

            if hasattr(series, "levels") and series.levels:
                log(f"  Pyramid levels: {len(series.levels)}")
                for i, level in enumerate(series.levels):
                    log(f"    Level {i}: shape={level.shape}")

        if tif.ome_metadata:
            log("  OME-XML metadata: present")
            # Parse and show channel names
            import xml.etree.ElementTree as ET

            try:
                root = ET.fromstring(tif.ome_metadata)
                ns = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}
                channels = root.findall(".//ome:Channel", ns)
                if channels:
                    log(f"  Channels in OME-XML: {len(channels)}")
                    for ch in channels[:5]:  # Show first 5
                        log(f"    - {ch.get('Name')}")
                    if len(channels) > 5:
                        log(f"    ... and {len(channels) - 5} more")
            except Exception as e:
                log(f"  Warning: Could not parse OME-XML: {e}")
        else:
            log("  WARNING: No OME-XML metadata found!")


def _scan_channel_metadata(input_dir: str):
    """Pass 1: discover channel files and scan their metadata (order, dtype,
    shape).
    """
    # Find all single-channel TIFF files
    channel_files = sorted(
        list(Path(input_dir).glob("*.tif")) + list(Path(input_dir).glob("*.tiff"))
    )

    if not channel_files:
        raise ValueError(f"No TIFF files found in {input_dir}")

    log(f"Found {len(channel_files)} channel files")

    # PASS 1: Collect metadata
    log("-" * 50)
    log("Pass 1: Scanning channels for metadata...")
    channel_names = []
    channel_colors = []
    height, width, dtype = None, None, None

    for i, channel_file in enumerate(channel_files):
        channel_name = channel_file.stem
        channel_names.append(channel_name)
        color = generate_channel_color(channel_name, i)
        channel_colors.append(color)
        log(f"  [{i}] {channel_name}: RGB{color}")

        with tifffile.TiffFile(str(channel_file)) as tif:
            page = tif.pages[0]
            if height is None:
                if len(page.shape) == 2:
                    height, width = page.shape
                else:
                    height, width = page.shape[-2:]
                dtype = page.dtype
                log(f"  Image dimensions: {height} x {width}, dtype: {dtype}")
            else:
                if len(page.shape) == 2:
                    h, w = page.shape
                else:
                    h, w = page.shape[-2:]
                if (h, w) != (height, width):
                    raise ValueError(f"Dimension mismatch: {channel_name}")

    num_output_channels = len(channel_names)
    log(f"Total output channels: {num_output_channels}")

    return (
        channel_files,
        channel_names,
        channel_colors,
        height,
        width,
        dtype,
        num_output_channels,
    )


def _channel_source(channel_files, height, width, dtype):
    """Pass 2's replacement: a lazy view over the channel files, not a loaded stack.

    `_load_channel_stack` used to allocate `(C, H, W)` here and fill it channel by channel
    -- ~19 GB on an 8-channel 40000x30000 uint16 slide, before the writer had allocated
    anything. Nothing is read by this call; the pixels arrive one row band at a time while
    the pyramid is written.
    """
    log("-" * 50)
    log("Pass 2/3: Streaming channels into the pyramidal OME-TIFF...")

    source = _ChannelFileSource(channel_files, height, width, dtype)
    # One validator per channel, folded during the base pass and emitted, once per channel,
    # after that channel's last band -- the same lines `_load_channel_stack` emitted after
    # its last whole-array read, with the percentages computed against the same denominator.
    validators = [_ChannelValidator(Path(f).stem, dtype) for f in source.channel_files]
    return source, validators


def _open_mask_source(height, width, masks_dir):
    """Open the optional cell/nuclei mask pair that becomes a second OME series.

    Read only when masks_dir contains both expected files; a single full-resolution uint32
    series, never pyramided/downsampled. Nothing is loaded here either -- the shapes are
    taken from the TIFF headers so the mismatch check still fails fast, and the pixels are
    streamed band by band by `_MaskFileSource`.
    """
    if not masks_dir:
        return None, None

    cell_matches = sorted(Path(masks_dir).glob("*cell_mask.tif"))
    nuclei_matches = sorted(Path(masks_dir).glob("*nuclei_mask.tif"))
    if not cell_matches or not nuclei_matches:
        # --masks-dir is only passed when the mask series is REQUIRED
        # (embed_masks + expanded compartment quant). Missing masks here is a
        # hard error, not a silent skip — surface it with the dir contents.
        found = [p.name for p in Path(masks_dir).glob("*")]
        raise ValueError(
            f"--masks-dir '{masks_dir}' given but no *cell_mask.tif / *nuclei_mask.tif found. "
            f"Directory contains: {found}. Expected SEGMENT outputs like <patient>_cell_mask.tif."
        )
    cell_mask_path, nuclei_mask_path = cell_matches[0], nuclei_matches[0]

    log(f"Loading masks for second OME series from: {masks_dir}")
    shapes = []
    for path in (cell_mask_path, nuclei_mask_path):
        with tifffile.TiffFile(str(path)) as tif:
            page = tif.pages[0]
            shapes.append(tuple(page.shape[-2:]))

    if shapes[0] != (height, width) or shapes[1] != (height, width):
        raise ValueError(
            "Mask/intensity dimension mismatch: intensity is "
            f"{(height, width)}, cell_mask is {shapes[0]}, "
            f"nuclei_mask is {shapes[1]}"
        )

    mask_source = _MaskFileSource(cell_mask_path, nuclei_mask_path, height, width)
    mask_names = ["cell_mask", "nuclei_mask"]
    log(f"  Mask stack shape: {mask_source.shape}, dtype: {mask_source.dtype}")
    return mask_source, mask_names


def _write_pyramid(
    output_path,
    source,
    channel_names,
    channel_colors,
    physical_size_x,
    physical_size_y,
    pyramid_resolutions,
    pyramid_scale,
    tile_size,
    compression,
    compressionargs,
    mask_source,
    mask_names,
    validators,
):
    """Pass 3: write the pyramidal OME-TIFF to a temp path (atomic write:
    write tmp → validate → rename is completed by the caller).
    """
    # Create output directory
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # PASS 3: Write pyramidal OME-TIFF (atomic: write tmp → validate → rename)
    # If the process is killed mid-write, only the .tmp file exists.
    # Nextflow won't find the expected output → marks as failed → retries.
    log("-" * 50)
    log("Pass 3: Writing pyramidal OME-TIFF...")
    tmp_path = output_path + ".tmp"

    write_pyramidal_ome_tiff_from_source(
        source,
        tmp_path,
        channel_names=channel_names,
        channel_colors=channel_colors,
        physical_size_x=physical_size_x,
        physical_size_y=physical_size_y,
        pyramid_resolutions=pyramid_resolutions,
        pyramid_scale=pyramid_scale,
        tile_size=tile_size,
        compression=compression,
        compressionargs=compressionargs,
        mask_source=mask_source,
        mask_names=mask_names,
        validators=validators,
    )

    return tmp_path


def _validate_written_pyramid(tmp_path, num_output_channels, channel_names, mask_shape):
    """Post-write validation: check the file has the right number of channels
    and is readable, including the optional mask series.
    """
    # Validate: check the file has the right number of channels and is readable.
    # Reads only the first tile per channel (~KB each), not full pages.
    # NOTE: use tif.series[0] (the intensity series) rather than raw tif.pages,
    # since an optional mask series (Image:1) adds its own top-level pages that
    # must not be counted against the intensity channel total.
    #
    # `mask_shape` is the expected (2, H, W), not the mask array: the masks are streamed
    # now, so there is no array left to compare against by the time this runs.
    log("Validating written file...")
    with tifffile.TiffFile(tmp_path) as tif:
        base_pages = list(tif.series[0].pages)
        n_written = len(base_pages)
        if n_written != num_output_channels:
            os.remove(tmp_path)
            raise RuntimeError(
                f"Pyramid validation failed: expected {num_output_channels} channels, "
                f"got {n_written} pages. File was likely truncated."
            )
        for i, page in enumerate(base_pages):
            # Read a single tile (first one) to verify the page is intact
            try:
                seg = page.asarray(maxworkers=1)[:256, :256]
                del seg
            except Exception as e:
                os.remove(tmp_path)
                raise RuntimeError(
                    f"Pyramid validation failed: channel {i} ({channel_names[i]}) "
                    f"is unreadable: {e}"
                )

        if mask_shape is not None:
            if len(tif.series) < 2:
                os.remove(tmp_path)
                raise RuntimeError(
                    "Pyramid validation failed: mask series was requested but "
                    "no second OME series was found in the written file."
                )
            mask_series = tif.series[1]
            if mask_series.dtype != np.uint32 or tuple(mask_series.shape) != tuple(
                mask_shape
            ):
                os.remove(tmp_path)
                raise RuntimeError(
                    "Pyramid validation failed: mask series shape/dtype mismatch "
                    f"(expected {tuple(mask_shape)} uint32, got {mask_series.shape} {mask_series.dtype})"
                )
            try:
                seg = mask_series.pages[0].asarray(maxworkers=1)[:256, :256]
                del seg
            except Exception as e:
                os.remove(tmp_path)
                raise RuntimeError(
                    f"Pyramid validation failed: mask series is unreadable: {e}"
                )
    log(
        f"  Validation passed: {n_written} channels intact"
        + (
            f", mask series {tuple(mask_shape)} intact"
            if mask_shape is not None
            else ""
        )
    )


def merge_channels(
    input_dir: str,
    output_path: str,
    physical_size_x: float = 0.325,
    physical_size_y: float = 0.325,
    pyramid_resolutions: int = 5,
    pyramid_scale: int = 2,
    tile_size: int = 512,
    compression: str = "zstd",
    compressionargs: Optional[Dict] = None,
    masks_dir: Optional[str] = None,
):
    """
    Merge all single-channel TIFF files into a single pyramidal OME-TIFF.
    """
    log("=" * 70)
    log("MERGE CHANNELS - Pyramidal OME-TIFF (QuPath Compatible)")
    log("=" * 70)
    log(f"Input directory: {input_dir}")
    log(f"Output: {output_path}")
    log(f"Pyramid: {pyramid_resolutions} levels, scale factor {pyramid_scale}")

    (
        channel_files,
        channel_names,
        channel_colors,
        height,
        width,
        dtype,
        num_output_channels,
    ) = _scan_channel_metadata(input_dir)

    source, validators = _channel_source(channel_files, height, width, dtype)

    mask_source, mask_names = _open_mask_source(height, width, masks_dir)

    try:
        tmp_path = _write_pyramid(
            output_path,
            source,
            channel_names,
            channel_colors,
            physical_size_x,
            physical_size_y,
            pyramid_resolutions,
            pyramid_scale,
            tile_size,
            compression,
            compressionargs,
            mask_source,
            mask_names,
            validators,
        )
    finally:
        source.release()
        if mask_source is not None:
            mask_source.release()

    _validate_written_pyramid(
        tmp_path,
        num_output_channels,
        channel_names,
        None if mask_source is None else mask_source.shape,
    )

    # Atomic rename — same filesystem, so this is instantaneous
    os.replace(tmp_path, output_path)

    gc.collect()

    # Summary
    file_size = Path(output_path).stat().st_size / (1024 * 1024 * 1024)
    log("=" * 70)
    log(f"SUCCESS: {output_path}")
    log(f"  Size: {file_size:.2f} GB")
    log(f"  Channels: {num_output_channels}")
    log(f"  Channel list: {', '.join(channel_names)}")
    log("=" * 70)

    return channel_names


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Merge single-channel TIFFs into pyramidal OME-TIFF (QuPath compatible)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input-dir", required=True, help="Directory with single-channel TIFF files"
    )
    parser.add_argument("--output", required=True, help="Output OME-TIFF path")
    parser.add_argument(
        "--masks-dir",
        help="Directory containing cell_mask.tif and nuclei_mask.tif "
        "to embed as a second (uint32, single-resolution) OME series",
    )
    parser.add_argument(
        "--physical-size-x",
        type=float,
        default=0.325,
        help="Pixel size in X (micrometers, default: 0.325)",
    )
    parser.add_argument(
        "--physical-size-y",
        type=float,
        default=0.325,
        help="Pixel size in Y (micrometers, default: 0.325)",
    )
    parser.add_argument(
        "--pyramid-resolutions",
        type=int,
        default=8,
        help="Number of pyramid levels (default: 8)",
    )
    parser.add_argument(
        "--pyramid-scale",
        type=int,
        default=2,
        help="Downscaling factor between levels (default: 2)",
    )
    parser.add_argument(
        "--tile-size", type=int, default=512, help="Tile size (default: 512)"
    )
    parser.add_argument(
        "--compression",
        type=str,
        default="zstd",
        choices=["lzw", "zlib", "zstd", "jpeg", "none"],
        help="Compression algorithm (default: zstd)",
    )
    return parser.parse_args()


def main() -> int:
    """Run merge and pyramid CLI."""
    configure_logging()
    # The old `zarr available: {HAS_ZARR}` line is gone with the probe behind it. It
    # reported whether an OPTIONAL fast path was open; the lazy read is mandatory now, so
    # a missing zarr is an ImportError at the first read rather than a line in the log.
    args = parse_args()
    compression = None if args.compression == "none" else args.compression

    # Set compression-specific arguments
    compressionargs = None
    if compression == "zstd":
        compressionargs = {"level": 2}  # Fast decompression for QuPath

    try:
        merge_channels(
            args.input_dir,
            args.output,
            physical_size_x=args.physical_size_x,
            physical_size_y=args.physical_size_y,
            pyramid_resolutions=args.pyramid_resolutions,
            pyramid_scale=args.pyramid_scale,
            tile_size=args.tile_size,
            compression=compression,
            compressionargs=compressionargs,
            masks_dir=args.masks_dir,
        )
        log("Complete!")
        return 0
    except Exception as e:
        log(f"Error: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
