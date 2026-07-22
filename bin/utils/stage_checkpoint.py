"""Pre-micro registration checkpoint — the one artifact staged QC cannot do without.

VALIS composes its stages destructively. ``register_micro`` updates each slide with
``fwd_dxdy = fwd_dxdy + micro_residual`` (registration.py) and writes the result back onto the
same attribute, so once micro-registration has run the registrar pickle holds a single field
that is *rigid + non-rigid + micro* and no longer knows what *rigid + non-rigid* looked like.
Nothing a reader can do recovers it.

So REGISTER takes a snapshot at the only moment the intermediate exists: after ``register()``
returns and before ``register_micro()`` is called. At that instant ``slide.M`` is already the
final rigid transform — ``MicroRigidRegistrar`` runs *inside* ``register()``, before the
non-rigid stage — which is why only the displacement field has to be saved and the staged
warps can all share one M.

The snapshot is per-slide, at non-rigid registration resolution (a few thousand pixels a side,
capped by ``max_non_rigid_registration_dim_px``), not slide resolution. Tens of MB, not tens
of GB. Reference slides have no field and are recorded as such.

Writing this must never be able to fail a registration: it is QC input, and QC in this pipeline
is non-gating. :func:`write_checkpoint` returns a status dict instead of raising.
"""
from __future__ import annotations

import json
import os

import numpy as np

MANIFEST_NAME = "stage_checkpoint.json"
CHECKPOINT_VERSION = 1


def _field_filename(slide_name):
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(slide_name))
    return f"{safe}.fwd_dxdy.npz"


def write_checkpoint(registrar, out_dir, micro_registration=True) -> dict:
    """Snapshot every slide's pre-micro forward displacement field into ``out_dir``.

    Parameters
    ----------
    registrar : valis.registration.Valis
        Must be in its post-``register()``, pre-``register_micro()`` state.
    micro_registration : bool
        Whether micro-registration is *about to* run. When it is not, the field saved here is
        also the final field, and the staged QC collapses ``non_rigid`` and ``micro`` into one
        stage rather than reporting a difference that cannot exist.

    Returns
    -------
    dict
        The manifest as written, plus an ``errors`` list naming any slide that could not be
        snapshotted. Callers log it; nothing here raises.
    """
    from valis_stage_warp import to_numpy_field

    os.makedirs(out_dir, exist_ok=True)
    slides, errors = {}, []
    for name, slide in registrar.slide_dict.items():
        entry = {"M": None, "field": None, "field_shape": None}
        try:
            if getattr(slide, "M", None) is not None:
                entry["M"] = np.asarray(slide.M, dtype=float).tolist()
            field = to_numpy_field(getattr(slide, "fwd_dxdy", None))
            if field is not None:
                fname = _field_filename(name)
                np.savez_compressed(os.path.join(out_dir, fname), fwd_dxdy=field)
                entry["field"] = fname
                entry["field_shape"] = [int(d) for d in field.shape]
        except Exception as e:  # noqa: BLE001 — QC input, must not fail registration
            errors.append(f"{name}: {type(e).__name__}: {e}")
        slides[str(name)] = entry

    manifest = {
        "version": CHECKPOINT_VERSION,
        "stage": "post_non_rigid_pre_micro",
        "micro_registration": bool(micro_registration),
        "slides": slides,
        "errors": errors,
    }
    with open(os.path.join(out_dir, MANIFEST_NAME), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def set_micro_registration(out_dir, ran: bool) -> bool:
    """Correct the manifest's ``micro_registration`` flag after the fact.

    The fields have to be captured *before* ``register_micro`` runs, but whether it ran can
    only be known after — VALIS's micro stage is wrapped in a caught exception in
    ``bin/register.py`` and continues on failure. If micro did not actually run, the saved
    field is also the final field, and a QC report claiming a distinct ``micro`` stage would
    be reporting the same warp twice. Returns whether the manifest was updated.
    """
    manifest_f = os.path.join(str(out_dir), MANIFEST_NAME)
    try:
        with open(manifest_f) as f:
            manifest = json.load(f)
        manifest["micro_registration"] = bool(ran)
        with open(manifest_f, "w") as f:
            json.dump(manifest, f, indent=2)
        return True
    except Exception:  # noqa: BLE001 — QC input, must not fail registration
        return False


class StageCheckpoint:
    """Reader for a directory written by :func:`write_checkpoint`."""

    def __init__(self, manifest, root):
        self.manifest = manifest
        self.root = root
        self._cache: dict = {}

    @classmethod
    def load(cls, path) -> "StageCheckpoint":
        """Load from a checkpoint directory or directly from its manifest file."""
        path = str(path)
        if os.path.isdir(path):
            root, manifest_f = path, os.path.join(path, MANIFEST_NAME)
        else:
            root, manifest_f = os.path.dirname(os.path.abspath(path)) or ".", path
        with open(manifest_f) as f:
            manifest = json.load(f)
        version = manifest.get("version")
        if version != CHECKPOINT_VERSION:
            raise ValueError(
                f"stage checkpoint version {version!r} != {CHECKPOINT_VERSION}; it was written "
                "by a different REGISTER than the one this QC expects")
        return cls(manifest, root)

    @property
    def micro_registration(self) -> bool:
        """Whether micro-registration ran after this snapshot was taken."""
        return bool(self.manifest.get("micro_registration", True))

    @property
    def slide_names(self):
        return list(self.manifest.get("slides", {}).keys())

    def has_slide(self, slide_name) -> bool:
        return str(slide_name) in self.manifest.get("slides", {})

    def fwd_dxdy(self, slide_name):
        """The slide's pre-micro forward field, or None if it had none (e.g. the reference)."""
        name = str(slide_name)
        if name in self._cache:
            return self._cache[name]
        entry = self.manifest.get("slides", {}).get(name)
        if entry is None:
            raise KeyError(
                f"slide {name!r} is not in the stage checkpoint (it has: {self.slide_names})")
        field = None
        if entry.get("field"):
            with np.load(os.path.join(self.root, entry["field"])) as npz:
                field = npz["fwd_dxdy"]
        self._cache[name] = field
        return field
