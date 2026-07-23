"""Read a page-stacked TIFF as ONE multi-band pyvips image. pyvips ONLY -- no ``valis`` import.

Why this is its own module
--------------------------
Both writers in this pipeline store a slide's C channels as C vertically-stacked PAGES with
``page-height=H`` -- valis ``slide_io.save_ome_tiff`` (``arrayjoin(bandsplit(), across=1)``) and
``reg_finalize._save_ome_pyvips`` alike. pyvips' default ``n=1`` therefore reads CHANNEL 0 ALONE,
which silently turned several "max|delta|=0.0" results on this branch into single-channel
comparisons, and made two others structurally unable to pass. ``n=-1`` on its own is NOT enough
either: it yields the ``(C*H, W)`` page stack. The band-join is the part that matters, so it gets
exactly ONE definition and everything that reads a written slide comes through here.

It lives apart from ``mirage_slide_reader`` (which re-exports it) because importing ``valis`` drags
in the JVM sniff, numba and matplotlib cache setup that ``bin/reg_finalize.py`` has to prepare the
environment for. Lightweight consumers -- ``bin/compare_registration.py`` -- need the band-join and
nothing else, and should not be able to fail for a reason that belongs to the registration stack.
"""
import pyvips

# n=-1 asks libvips for every page joined vertically: a "toilet roll" of height C*page_height.
TOILET_ROLL = -1


def open_roll(src_f):
    """Open every page of `src_f` as one tall (C*H, W) image, lazily."""
    return pyvips.Image.new_from_file(str(src_f), access="random", n=TOILET_ROLL)


def page_height(img):
    """Height of a single page, or the whole image when the tag is absent (single-page)."""
    try:
        return int(img.get("page-height"))
    except pyvips.Error:
        return int(img.height)


def bandjoin_pages(img):
    """Turn a vertically-packed multi-page image into a multi-band image (lazy)."""
    ph = page_height(img)
    n_pages = img.height // ph
    if n_pages <= 1:
        return img
    bands = [img.crop(0, i * ph, img.width, ph) for i in range(n_pages)]
    return bands[0].bandjoin(bands[1:])


def open_multiband(src_f):
    """Open a possibly page-stacked TIFF as ONE lazy multi-band image, shape (H, W, C)."""
    return bandjoin_pages(open_roll(src_f))
