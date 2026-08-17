#!/usr/bin/env python3
"""Apply nf-core BASICPY's illumination profiles and reassemble the slide.

nf-core's ``basicpy`` module computes PROFILES ONLY. Upstream, mcmicro applies them
downstream inside ASHLAR; mirage has no ASHLAR, so this script is the missing half::

    corrected = (image - darkfield) / flatfield

per channel, per pseudo-FOV, followed by reassembly from the sidecar
``bin/tile_for_basic.py`` wrote.

WHAT IS PRESERVED FROM THE IN-PROCESS BaSiC PATH, and where it comes from:

* **The fiducial skip.** Not re-decided here. ``tile_for_basic`` resolved it once through
  ``utils.metadata.is_nuclear`` and recorded ``corrected_channels`` in the sidecar; this
  script applies profiles to exactly those channels and copies the rest through. Making
  the decision in one place is what stops the two halves drifting -- the drift that
  ``bin/preprocess.py``'s comment records (an earlier version tested ``"DAPI" in
  name.upper()`` and silently corrected a configured CELLTOX fiducial).

* **The negative clip.** ``bin/utils/validation.py``'s ``clip_negative_values``, called
  ONCE on the assembled stack, so its percentage is computed against the whole image and
  it emits exactly one aggregate line. Not forked, not re-implemented, and deliberately
  not called per channel: ``bin/split_multichannel.py``'s ``_StreamingNegativeClip``
  exists only because that script never holds the whole array at once. This one does --
  the same ``np.stack`` the in-process path built before casting -- so it can just call
  the real function.

  Its RATIONALE has changed and the old one is not carried over. ``bin/preprocess.py``
  explained the clip by BaSiC's darkfield exceeding a pixel value; the pipeline now runs
  BASICPY at upstream defaults, where ``get_darkfield`` is False, so no additive offset is
  subtracted and that specific mechanism is largely gone. What is true today: dividing by
  a fitted flatfield cannot make a non-negative pixel negative, so on the shipped
  configuration this clip is expected to find nothing -- it is kept because the contract
  is what downstream reads, and because this script still subtracts whatever darkfield it
  is handed (a real one when run standalone, or if the defaults decision is revisited).

  What it does NOT do is catch a degenerate profile. `nan < 0` is False, so
  ``detect_negative_values`` never sees one; that is ``_read_profile_stack``'s job, and it
  rejects non-finite entries and a non-positive flatfield BEFORE any arithmetic runs.

* **The storage dtype rule.** Clip to the dtype's range, ROUND (half-to-even), then cast.
  ``.astype()`` truncates toward zero, which is a one-sided -0.5 LSB bias on every
  non-integral pixel and never averages out. Same rule, same words, as
  ``merge_channels_pyramid.to_uint16``; written out here rather than imported because
  that module lives in a different container image and pulls a different dependency set.

THE PROFILE STACK'S SHAPE. ``/opt/main.py`` builds ``np.array(flatfields)`` -- one 2-D
plane per FITTED channel, in tile-stack order -- and writes it with ``ome=True``. So its
C axis is indexed by position in ``profile_channels``, not by source channel index, and
the two differ exactly when a fiducial was skipped. tifffile drops a singleton leading
axis on read, so a single-channel profile comes back 2-D; ``_read_profile_stack``
normalises that rather than letting it broadcast silently, and checks the tile shape --
a 128x128 working-size profile would otherwise broadcast or fail with a NumPy shape error
instead of saying that the profiles belong to a different tile grid.

THE DARKFIELD IS ZERO ON THE SHIPPED CONFIGURATION. ``conf/modules.config`` runs BASICPY
at upstream defaults, where ``get_darkfield`` is False. basicpy then never updates its
``D_R``/``D_Z`` terms from their zero initialisation, and ``fit`` still does
``self.darkfield = skimage_resize(D, images.shape[1:])`` -- so ``*-dfp.ome.tif`` is
PRESENT, correctly shaped, and all zeros. That is detected and logged; a missing file is
also accepted and treated as zero. Neither is assumed: the correction is reported as
flatfield-only rather than described as though an offset had been removed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np  # noqa: E402
import tifffile  # noqa: E402
from fov_tiling import reconstruct_image_from_fovs, split_image_into_fovs  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from pixel_size import read_ome_pixel_size, warn_on_pixel_size_mismatch  # noqa: E402
from tiled_io import open_lazy  # noqa: E402
from validation import clip_negative_values, log_image_stats  # noqa: E402

logger = get_logger(__name__)

__all__ = ["apply_basic_profiles", "main"]

#: The sidecar layouts this script understands. Refusing an unknown one beats misreading
#: a renamed field and silently correcting the wrong channels.
SUPPORTED_SIDECAR_VERSIONS = (1,)

STAGE = "apply_basic_profiles"


def _read_profile_stack(path, n_expected, tile_shape, label, require_positive=False):
    """Read a ``/opt/main.py`` profile file as ``(n_expected, tile_h, tile_w)`` float64.

    Validates the VALUES, not only the shape. A degenerate profile is silent all the way
    to the published slide: ``x / 0`` is ``inf``, ``0 / 0`` and ``nan - nan`` are ``nan``,
    and neither is caught downstream -- ``clip_negative_values`` -> ``detect_negative_values``
    tests ``data < 0``, which is False for both, and ``np.round(np.clip(nan)).astype(uint16)``
    is undefined behaviour that yields a plausible-looking integer. So the run stays green
    and the pixels are garbage.

    ``require_positive`` is for the flatfield: it is a normalised gain, strictly positive
    by construction (basicpy divides ``S`` by its own mean and raises outright if
    ``S.max() == 0``). A zero or negative entry means the fit failed, or that the
    flatfield and darkfield arrived the wrong way round -- at upstream defaults the
    darkfield is ALL ZEROS, so a swapped pair lands here as an all-zero flatfield and is
    refused rather than dividing every pixel by zero.
    """
    data = np.asarray(tifffile.imread(str(path)))
    if data.ndim == 2:
        # tifffile drops the singleton leading axis of a one-channel stack.
        data = data[np.newaxis, ...]
    if data.ndim != 3:
        raise ValueError(
            f"{path}: expected a 2-D or 3-D {label} profile stack, got shape {data.shape}"
        )
    if data.shape[0] != n_expected:
        raise ValueError(
            f"{path}: {label} profile stack has {data.shape[0]} channel(s) but the "
            f"sidecar says {n_expected} channel(s) were fitted. The profiles do not "
            "belong to this tile stack."
        )
    if tuple(data.shape[1:]) != tuple(tile_shape):
        raise ValueError(
            f"{path}: {label} profile tile shape {tuple(data.shape[1:])} does not match "
            f"the sidecar's tile shape {tuple(tile_shape)}. The profiles belong to a "
            "different tile grid; refusing to broadcast them onto this one."
        )

    data = data.astype(np.float64, copy=False)

    if not np.all(np.isfinite(data)):
        n_bad = int(np.count_nonzero(~np.isfinite(data)))
        raise ValueError(
            f"{path}: {n_bad} value(s) in the {label} profile are not finite (NaN or "
            "inf). Applying them would produce NaN pixels that every downstream check "
            "passes -- `nan < 0` is False, so the negative clip never sees them."
        )

    if require_positive and data.min() <= 0:
        raise ValueError(
            f"{path}: the {label} profile must be strictly positive (min is "
            f"{data.min():.6g}); it is a normalised gain and dividing by a zero or "
            "negative entry is silent. If this is BASICPY's *-dfp file, the flatfield "
            "and darkfield have been swapped: at upstream defaults the darkfield is all "
            "zeros."
        )

    return data


def _to_storage_dtype(data, dtype):
    """Clip to the dtype's range, ROUND, then cast -- never a bare ``.astype()``.

    ``.astype()`` truncates toward zero. On intensity data that later becomes a measured
    per-cell statistic that is a systematic downward bias of up to one count, mean -0.5
    LSB, one-sided, and it never averages out. Clipping stays ahead of the round because
    65535.6 rounds to 65536, which wraps to 0 in uint16.
    """
    if data.dtype == dtype:
        return data
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.round(np.clip(data, info.min, info.max)).astype(dtype)
    return data.astype(dtype)


def apply_basic_profiles(
    image_path,
    sidecar_path,
    flatfield_path,
    darkfield_path,
    output_path,
    pixel_size=0.325,
):
    """Correct ``image_path`` with the fitted profiles and write the reassembled slide."""
    manifest = json.loads(Path(sidecar_path).read_text())
    version = manifest.get("format_version")
    if version not in SUPPORTED_SIDECAR_VERSIONS:
        raise ValueError(
            f"{sidecar_path}: format_version {version!r} is not one of "
            f"{SUPPORTED_SIDECAR_VERSIONS}. Refusing to guess what its fields mean."
        )

    channel_names = list(manifest["channel_names"])
    profile_channels = list(manifest["profile_channels"])
    corrected_channels = set(manifest["corrected_channels"])
    positions = [tuple(p) for p in manifest["positions"]]
    height, width = manifest["image_shape"]
    n_fovs_y, n_fovs_x = manifest["n_fovs_y"], manifest["n_fovs_x"]
    storage_dtype = np.dtype(manifest["source_dtype"])

    tile_shape = tuple(manifest["tile_shape"])

    flatfields = _read_profile_stack(
        flatfield_path, len(profile_channels), tile_shape, "flatfield",
        require_positive=True,
    )
    if darkfield_path is None or not Path(darkfield_path).exists():
        # Handled, not assumed. The vendored module always declares the file as an output,
        # but the script is hand-runnable and a caller may legitimately have none.
        darkfields = np.zeros((len(profile_channels), *tile_shape), dtype=np.float64)
        logger.info(
            "No darkfield file supplied -- correction is flatfield-only "
            "(image / flatfield), no additive offset is removed."
        )
    else:
        darkfields = _read_profile_stack(
            darkfield_path, len(profile_channels), tile_shape, "darkfield"
        )
        if not np.any(darkfields):
            # The SHIPPED case: conf/modules.config runs BASICPY at upstream defaults,
            # where get_darkfield is False, so basicpy resizes its zero-initialised
            # darkfield to the tile shape and the file is all zeros. Say so, rather than
            # logging a correction that implies an offset was subtracted.
            logger.info(
                f"{Path(darkfield_path).name} is all zeros (BASICPY ran with "
                "get_darkfield=False, its default) -- no darkfield was estimated, so the "
                "correction is flatfield-only (image / flatfield)."
            )
        else:
            logger.info(
                f"Subtracting a fitted darkfield (min={darkfields.min():.4g}, "
                f"max={darkfields.max():.4g}) before dividing by the flatfield."
            )

    # Where each source channel's profile sits on the profile stack's C axis.
    profile_index = {source: k for k, source in enumerate(profile_channels)}

    detected_x, detected_y = read_ome_pixel_size(image_path)
    warn_on_pixel_size_mismatch(
        (detected_x, detected_y),
        pixel_size,
        source=Path(image_path).name,
        logger=logger,
    )
    pixel_size_x = detected_x if detected_x is not None else pixel_size
    pixel_size_y = detected_y if detected_y is not None else pixel_size

    logger.info(
        f"Applying profiles to {len(corrected_channels)}/{len(channel_names)} channel(s) "
        f"over {n_fovs_y}x{n_fovs_x} pseudo-FOVs"
    )

    arr, _dtype, close = open_lazy(image_path)
    channels = []
    try:
        for index, name in enumerate(channel_names):
            channel = np.asarray(arr[index, :, :])
            if index not in corrected_channels:
                logger.debug(f"  [SKIP] {name!r} (nuclear/fiducial, or nothing fitted)")
                channels.append(channel)
                continue

            k = profile_index[index]
            fov_stack, _pos, _shape = split_image_into_fovs(channel, n_fovs_x, n_fovs_y)
            corrected_fovs = (
                fov_stack.astype(np.float32) - darkfields[k]
            ) / flatfields[k]
            channels.append(
                reconstruct_image_from_fovs(
                    corrected_fovs.astype(np.float32), positions, (height, width)
                )
            )
            logger.debug(f"  [OK] {name!r} corrected")
    finally:
        close()

    # np.stack promotes the mix of corrected float32 and untouched integer channels to
    # float32 -- exactly what the in-process path's `np.stack(preprocessed_channels)` did
    # with BaSiC's float32 output beside its skipped uint16 channels.
    corrected = np.stack(channels, axis=0)

    # ONE aggregate line, over the whole image, from the shared function. See the module
    # docstring for why this is not the per-channel streaming aggregator.
    corrected = clip_negative_values(corrected, logger, stage_name=STAGE)

    corrected = _to_storage_dtype(corrected, storage_dtype)
    log_image_stats(corrected, "after_basic_correction", logger)

    metadata = {
        "axes": "CYX",
        "Channel": {"Name": channel_names[: corrected.shape[0]]},
        "PhysicalSizeX": pixel_size_x,
        "PhysicalSizeXUnit": "µm",
        "PhysicalSizeY": pixel_size_y,
        "PhysicalSizeYUnit": "µm",
    }
    tifffile.imwrite(
        str(output_path),
        corrected,
        photometric="minisblack",
        metadata=metadata,
        bigtiff=True,
        ome=True,
        compression="zlib",
        tile=(2048, 2048),
    )
    logger.info(f"[OK] Saved OME-TIFF with {corrected.shape[0]} channels to {output_path}")
    return corrected


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Apply BASICPY flatfield/darkfield profiles and reassemble the slide."
    )
    parser.add_argument("--image", required=True, help="Input OME-TIFF (C, Y, X).")
    parser.add_argument("--sidecar", required=True, help="Tile-position JSON.")
    parser.add_argument("--flatfield", required=True, help="*-ffp.ome.tif from BASICPY.")
    # Optional: at upstream defaults BASICPY estimates no darkfield, and while it still
    # writes an all-zero *-dfp.ome.tif, a caller with none is a legitimate case.
    parser.add_argument(
        "--darkfield", default=None, help="*-dfp.ome.tif from BASICPY (optional)."
    )
    parser.add_argument("--output", required=True, help="Corrected OME-TIFF to write.")
    parser.add_argument("--pixel-size", dest="pixel_size", type=float, default=0.325)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(getattr(logging, args.log_level.upper(), logging.INFO))
    apply_basic_profiles(
        args.image,
        args.sidecar,
        args.flatfield,
        args.darkfield,
        args.output,
        pixel_size=args.pixel_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
