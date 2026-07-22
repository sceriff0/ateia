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
import re

import numpy as np
import pyvips

from valis import slide_io, slide_tools

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
        arr = np.frombuffer(img.write_to_memory(), dtype=slide_tools.VIPS_FORMAT_NUMPY_DTYPE[img.format])
        arr = arr.reshape(img.height, img.width, img.bands)
        return arr[..., 0] if img.bands == 1 else arr

    def scale_physical_size(self, level):
        return list(self.metadata.pixel_physical_size_xyu)

    # -- construction helpers --------------------------------------------------

    def _build_metadata(self):
        ome_xml, tile_wh = _read_tiff_header(self.src_f)
        md = slide_io.MetaData(os.path.basename(self.src_f), "mirage-pyvips", series=self.series)
        md.slide_dimensions = [(self._img.width, self._img.height)]
        md.n_channels = self._img.bands
        md.is_rgb = False
        md.channel_names = _channel_names(ome_xml, self._img.bands)
        md.pixel_physical_size_xyu = _physical_size(ome_xml)
        md.original_xml = ome_xml
        md.bf_datatype = slide_io.vips2bf_dtype(self._img.format)
        md.optimal_tile_wh = tile_wh
        return md

    @staticmethod
    def can_read(src_f):
        """True only for tiled BigTIFF, single-sample, non-pyramidal OME-TIFFs that mirage's
        own writers produce (bin/preprocess.py, bin/reg_finalize.py::_save_ome_pyvips).

        This is a safety gate, not a performance heuristic: a false negative merely falls
        back to the slower BioFormats/JVM path (get_reader_for), while a false positive would
        silently misdecode a third-party slide (e.g. an RGB scan or a pyramidal OME-TIFF) and
        corrupt registration. The broad ``except Exception: return False`` below is
        deliberate -- on ANY ambiguity or read error, fail toward the slower-but-correct path.
        """
        try:
            import tifffile
            with tifffile.TiffFile(str(src_f)) as tf:
                page = tf.pages[0]
                if not page.is_tiled:
                    return False
                if not tf.is_bigtiff:
                    return False
                if getattr(page, "subifds", None):
                    return False
                if page.samplesperpixel != 1:
                    return False
                ome_xml = tf.ome_metadata

            img = _open_roll(src_f)
            has_page_height = True
            try:
                img.get("page-height")
            except pyvips.Error:
                has_page_height = False
            if not ome_xml and not has_page_height:
                return False

            joined = _bandjoin_pages(img)
            if ome_xml:
                m = re.search(r'SizeC="(\d+)"', ome_xml)
                if m and joined.bands != int(m.group(1)):
                    return False
            return joined.width > 0 and joined.height > 0 and joined.bands >= 1
        except Exception:
            return False


def _read_tiff_header(src_f):
    """Single tifffile open: returns (ome_xml_or_None, tile_width)."""
    import tifffile
    with tifffile.TiffFile(str(src_f)) as tf:
        ome_xml = tf.ome_metadata
        tw = tf.pages[0].tilewidth
    return ome_xml, (int(tw) if tw else 1024)


def _channel_names(ome_xml, n_bands):
    if not ome_xml:
        return [f"C{i}" for i in range(n_bands)]
    names = re.findall(r'<Channel[^>]*\bName="([^"]*)"', ome_xml)
    if len(names) < n_bands:
        names = list(names) + [f"C{i}" for i in range(len(names), n_bands)]
    return names[:n_bands]


def _physical_size(ome_xml):
    if not ome_xml:
        return [1.0, 1.0, slide_io.PIXEL_UNIT]
    x = re.search(r'PhysicalSizeX="([^"]+)"', ome_xml)
    y = re.search(r'PhysicalSizeY="([^"]+)"', ome_xml)
    u = re.search(r'PhysicalSizeXUnit="([^"]+)"', ome_xml)
    if not (x and y):
        return [1.0, 1.0, slide_io.PIXEL_UNIT]
    return [float(x.group(1)), float(y.group(1)), u.group(1) if u else "µm"]


def get_reader_for(src_f, series=None):
    """Return the cheapest correct reader class for `src_f`.

    MirageVipsSlideReader when we recognise the file (no JVM, lazy); otherwise VALIS's own
    dispatch, which starts the JVM and picks BioFormats.

    ``MIRAGE_FORCE_BIOFORMATS=1`` forces VALIS's own dispatch regardless of ``can_read`` -- the
    only way an integration test can produce a genuine BioFormats reference to diff the lazy
    reader against (see tests/integration/verify_lowmem_bitidentical.py).
    """
    if os.environ.get("MIRAGE_FORCE_BIOFORMATS") == "1":
        return slide_io.get_slide_reader(str(src_f), series=series)
    if MirageVipsSlideReader.can_read(src_f):
        return MirageVipsSlideReader
    return slide_io.get_slide_reader(str(src_f), series=series)


def all_readable(paths):
    """True when every path can be read JVM-free (so the caller can skip init_jvm entirely).

    Also honours ``MIRAGE_FORCE_BIOFORMATS`` so callers that gate JVM startup on this (e.g.
    reg_finalize.main) actually start the JVM when the escape hatch is set, instead of skipping
    it and leaving get_reader_for to force BioFormats through a JVM that was never initialised.
    """
    if os.environ.get("MIRAGE_FORCE_BIOFORMATS") == "1":
        return False
    return all(MirageVipsSlideReader.can_read(p) for p in paths)
