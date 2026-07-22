"""JVM-free VALIS-compatible slide reader for mirage's own tiled OME-TIFF output.

Why this exists
---------------
VALIS routes multichannel OME-TIFF to ``BioFormatsSlideReader`` (valis slide_io, reader
dispatch), whose ``slide2vips`` decodes every tile through the JVM and stitches them --
materializing the whole decompressed slide. That is mirage's registration RAM wall.
VALIS's own comment says the reason is SPEED ("very slow for multichannel"), not correctness.

mirage always writes its own inputs: ``bin/preprocess.py`` emits tiled (2048x2048) BigTIFF
OME-TIFF, and ``bin/reg_finalize.py::_save_ome_pyvips`` writes tiled BigTIFF too. Those are
randomly addressable, so pyvips can read them LAZILY with no JVM. Since
``valis.warp_tools.warp_img`` is already pure lazy pyvips, handing VALIS a lazy reader makes
the whole warp stream in O(tile) RAM with no algorithm change.

Anything this reader does not recognise falls back to VALIS's own dispatch via
``get_reader_for``, so non-mirage inputs keep working exactly as before.
"""
import os

import numpy as np
import pyvips

from valis import slide_io

# Bands are packed vertically into pages by both tifffile (ome=True, axes="CYX") and
# reg_finalize._save_ome_pyvips (page-height set explicitly). Reading with n=-1 yields a
# "toilet roll" of height C*page_height which we crop back into bands.
_TOILET_ROLL = -1


def _open_roll(src_f):
    return pyvips.Image.new_from_file(str(src_f), access="random", n=_TOILET_ROLL)


def _page_height(img):
    try:
        return int(img.get("page-height"))
    except pyvips.Error:
        return int(img.height)


def _bandjoin_pages(img):
    """Turn a vertically-packed multi-page image into a multi-band image (lazy)."""
    ph = _page_height(img)
    n_pages = img.height // ph
    if n_pages <= 1:
        return img
    bands = [img.crop(0, i * ph, img.width, ph) for i in range(n_pages)]
    return bands[0].bandjoin(bands[1:])


class MirageVipsSlideReader(slide_io.SlideReader):
    """Lazy, JVM-free reader for mirage-produced tiled OME-TIFFs."""

    def __init__(self, src_f, series=None, *args, **kwargs):
        super().__init__(src_f, *args, **kwargs)
        self.src_f = str(src_f)
        self.series = 0 if series is None else int(series)
        self._img = _bandjoin_pages(_open_roll(self.src_f))
        self.metadata = self._build_metadata()

    # -- VALIS SlideReader API -------------------------------------------------

    def slide2vips(self, level=0, series=None, xywh=None, *args, **kwargs):
        if level != 0:
            # mirage's OME-TIFFs are single-level (bin/preprocess.py writes no subifds).
            raise ValueError(f"MirageVipsSlideReader has no pyramid level {level}")
        img = self._img
        if xywh is not None:
            x, y, w, h = (int(v) for v in xywh)
            img = img.crop(x, y, w, h)
        return img

    def slide2image(self, level=0, series=None, xywh=None, *args, **kwargs):
        img = self.slide2vips(level=level, series=series, xywh=xywh)
        arr = np.frombuffer(img.write_to_memory(), dtype=_vips_dtype(img.format))
        arr = arr.reshape(img.height, img.width, img.bands)
        return arr[..., 0] if img.bands == 1 else arr

    def scale_physical_size(self, level):
        return list(self.metadata.pixel_physical_size_xyu)

    # -- construction helpers --------------------------------------------------

    def _build_metadata(self):
        md = slide_io.MetaData(os.path.basename(self.src_f), "mirage-pyvips", series=self.series)
        md.slide_dimensions = [(self._img.width, self._img.height)]
        md.n_channels = self._img.bands
        md.is_rgb = False
        md.channel_names = _channel_names(self.src_f, self._img.bands)
        md.pixel_physical_size_xyu = _physical_size(self.src_f)
        md.original_xml = _ome_xml(self.src_f)
        md.bf_datatype = _bf_dtype(self._img.format)
        md.optimal_tile_wh = _tile_wh(self.src_f)
        return md

    @staticmethod
    def can_read(src_f):
        """True only for tiled TIFFs this reader is known to handle correctly."""
        try:
            import tifffile
            with tifffile.TiffFile(str(src_f)) as tf:
                page = tf.pages[0]
                if not page.is_tiled:
                    return False
            img = _bandjoin_pages(_open_roll(src_f))
            return img.width > 0 and img.height > 0 and img.bands >= 1
        except Exception:
            return False


def _vips_dtype(vips_format):
    return {
        "uchar": np.uint8, "char": np.int8, "ushort": np.uint16, "short": np.int16,
        "uint": np.uint32, "int": np.int32, "float": np.float32, "double": np.float64,
    }[vips_format]


def _bf_dtype(vips_format):
    return {
        "uchar": "uint8", "char": "int8", "ushort": "uint16", "short": "int16",
        "uint": "uint32", "int": "int32", "float": "float", "double": "double",
    }[vips_format]


def _ome_xml(src_f):
    import tifffile
    with tifffile.TiffFile(str(src_f)) as tf:
        return tf.ome_metadata


def _channel_names(src_f, n_bands):
    xml = _ome_xml(src_f)
    if not xml:
        return [f"C{i}" for i in range(n_bands)]
    import re
    names = re.findall(r'<Channel[^>]*\bName="([^"]*)"', xml)
    if len(names) < n_bands:
        names = list(names) + [f"C{i}" for i in range(len(names), n_bands)]
    return names[:n_bands]


def _physical_size(src_f):
    xml = _ome_xml(src_f)
    if not xml:
        return [1.0, 1.0, slide_io.PIXEL_UNIT]
    import re
    x = re.search(r'PhysicalSizeX="([^"]+)"', xml)
    y = re.search(r'PhysicalSizeY="([^"]+)"', xml)
    u = re.search(r'PhysicalSizeXUnit="([^"]+)"', xml)
    if not (x and y):
        return [1.0, 1.0, slide_io.PIXEL_UNIT]
    return [float(x.group(1)), float(y.group(1)), u.group(1) if u else "µm"]


def _tile_wh(src_f):
    import tifffile
    with tifffile.TiffFile(str(src_f)) as tf:
        tw = tf.pages[0].tilewidth
    return int(tw) if tw else 1024


def get_reader_for(src_f, series=None):
    """Return the cheapest correct reader class for `src_f`.

    MirageVipsSlideReader when we recognise the file (no JVM, lazy); otherwise VALIS's own
    dispatch, which starts the JVM and picks BioFormats.
    """
    if MirageVipsSlideReader.can_read(src_f):
        return MirageVipsSlideReader
    return slide_io.get_slide_reader(str(src_f), series=series)


def all_readable(paths):
    """True when every path can be read JVM-free (so the caller can skip init_jvm entirely)."""
    return all(MirageVipsSlideReader.can_read(p) for p in paths)
