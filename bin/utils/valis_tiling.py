"""Set/clear ``NonRigidTileRegistrar.EXTERNAL_TILE_HOOK`` — the explicit seam patched into VALIS
1.0.0 (spec §6 Option B, ``containers/valis/calc_hook.patch``) — to externalize the non-rigid tile
loop into separate Nextflow processes. This is the ONLY module that knows the seam.

Design (spec §6.1/§6.2/§6.3, all proven by the spikes):
  * ``install_halt_hook`` — used by REG_PREP. Dumps the tiler's inputs to disk, then raises
    ``TilesPending``. ``Valis.register()`` swallows the exception (Blocker 2) and returns ``None``;
    PREP detects the halt by the populated dump dir, NOT by catching the exception. PREP does NO
    tile compute on the fat JVM node.
  * ``install_dump_hook`` — fallback / non-decomposed mode: dump inputs, then run the real loop
    inline (``self._calc_tiles()``, the patched-out original body).
  * ``install_read_hook`` — load precomputed tiles and stitch via VALIS's own ``stitch_tiles``.
    Provided for an in-process tiled run; the decomposed REG_FINALIZE stitches directly instead
    (it never re-enters ``calc``), so this is not on the REG_PREP→REG_TILE→REG_FINALIZE path.

Dump contract (consumed by reg_tile.py; warp-state for reg_finalize.py is dumped by reg_prep.py
from the Valis/Slide objects, NOT here — the registrar ``self`` only has the tiler inputs):
  moving.v, fixed.v, mask.v (if any), target_stats.npy (if any), expanded_bboxes.npy, manifest.json
"""
import json
import os

import numpy as np


class TilesPending(Exception):
    """Raised by the halt hook after inputs are dumped, to stop PREP before tile compute."""


def _cls_path(cls):
    """Importable 'module:QualName' for a class (JSON-friendly; re-imported by reg_tile.py)."""
    if cls is None:
        return None
    return f"{cls.__module__}:{cls.__qualname__}"


def _dump_inputs(self, out_dir):
    """Persist everything REG_TILE needs to recompute a tile in a fresh, JVM-free process.

    ``self`` is the live ``NonRigidTileRegistrar`` mid-``calc``; its tiler inputs are already the
    pre-processed images at the non-rigid resolution. The grid (expanded_bboxes, n_rows/n_cols) is
    dumped verbatim so REG_TILE reproduces VALIS's exact tile geometry.
    """
    os.makedirs(out_dir, exist_ok=True)
    self.moving_img.write_to_file(os.path.join(out_dir, "moving.v"))
    self.fixed_img.write_to_file(os.path.join(out_dir, "fixed.v"))
    has_mask = getattr(self, "mask", None) is not None
    if has_mask:
        self.mask.write_to_file(os.path.join(out_dir, "mask.v"))
    has_target_stats = getattr(self, "target_stats", None) is not None
    if has_target_stats:
        np.save(os.path.join(out_dir, "target_stats.npy"), np.asarray(self.target_stats))
    np.save(os.path.join(out_dir, "expanded_bboxes.npy"), np.asarray(self.expanded_bboxes))

    manifest = {
        "n_tiles": int(self.n_tiles),
        "n_rows": int(self.n_rows),
        "n_cols": int(self.n_cols),
        "tile_wh": int(self.tile_wh),
        "tile_buffer": int(self.tile_buffer),
        "has_mask": has_mask,
        "has_target_stats": has_target_stats,
        # processing is done in PREP (src_f live, Blocker 3); REG_TILE runs processing_cls=None.
        "processing_cls": _cls_path(getattr(self, "processing_cls", None)),
        "processing_kwargs": getattr(self, "processing_kwargs", None),
        "non_rigid_registrar_cls": _cls_path(self.non_rigid_registrar_cls),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)


def install_halt_hook(out_dir):
    """REG_PREP default (§5C): dump inputs then RAISE — no tile compute on the fat JVM node.

    ``Valis.register()`` catches the raise and returns ``None`` (Blocker 2); PREP treats a populated
    ``out_dir`` (manifest.json present) as the halt signal.
    """
    def halt(self):
        _dump_inputs(self, out_dir)
        raise TilesPending(out_dir)

    _set_hook(halt)


def install_dump_hook(out_dir):
    """Fallback / single-node tiled mode: dump inputs, then run the real loop inline."""
    def dump(self):
        _dump_inputs(self, out_dir)
        return self._calc_tiles()  # the patched-out original tile loop

    _set_hook(dump)


def install_read_hook(tiles_dir):
    """Load precomputed per-tile fields and stitch via VALIS's own ``stitch_tiles``.

    For an in-process tiled run. The decomposed REG_FINALIZE stitches directly and does not use this.
    """
    import pyvips
    from valis import warp_tools

    def read(self):
        bk = [pyvips.Image.new_from_file(os.path.join(tiles_dir, f"bk_{i}.v"))
              for i in range(self.n_tiles)]
        fwd = [pyvips.Image.new_from_file(os.path.join(tiles_dir, f"fwd_{i}.v"))
               for i in range(self.n_tiles)]
        return (
            warp_tools.stitch_tiles(bk, self.expanded_bboxes, self.n_rows, self.n_cols, self.tile_buffer),
            warp_tools.stitch_tiles(fwd, self.expanded_bboxes, self.n_rows, self.n_cols, self.tile_buffer),
        )

    _set_hook(read)


def clear_hook():
    _set_hook(None)


def _set_hook(fn):
    from valis import non_rigid_registrars as nrr
    if not hasattr(nrr.NonRigidTileRegistrar, "EXTERNAL_TILE_HOOK"):
        raise RuntimeError(
            "VALIS image is missing the EXTERNAL_TILE_HOOK seam (containers/valis/calc_hook.patch "
            "not applied). Rebuild mirage-valis:1.0.0.")
    nrr.NonRigidTileRegistrar.EXTERNAL_TILE_HOOK = fn
