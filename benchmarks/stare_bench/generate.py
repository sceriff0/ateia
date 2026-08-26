"""Compose crop -> ground-truth field -> warp -> physics -> a scored pair.

Pure given a seed. Writes ref/mov OME-TIFFs plus a truth.json carrying
everything a metric needs and everything a reader needs to reproduce the data:
seed, generator_version, param_hash, the field's exact parameters, landmarks,
per-tile registrability labels, and the physics configuration.

At the gigapixel rung the dense field is NOT written -- a (50000, 50000, 2)
float32 array is ~20 GB, and materialising it would break the <=8 GB claim the
top rung exists to prove. Ground truth is recovered from
{seed, field_family, field_params} instead, which is exact because this module
is pure.

``tifffile.imwrite(..., ome=True)`` stamps a random ``uuid1()`` into the
OME-XML header by default, which would break byte-identical determinism under
a fixed seed; the OME UUID is therefore derived deterministically from
``param_hash`` (see ``_stable_uuid``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import numpy as np

from . import __version__
from .fields import DENSE_MAX_PIXELS, make_field
from .labels import tile_labels
from .physics import apply_all
from .texture import SyntheticCropSource, describe

__all__ = ["generate_pair", "param_hash"]


def param_hash(payload):
    """Stable short hash over every generator input."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=float)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _stable_uuid(param_hash_value, tag):
    """A deterministic OME-XML UUID.

    ``tifffile.imwrite(..., ome=True)`` stamps a random ``uuid1()`` into the
    OME-XML header on every call, which alone would make two runs of this
    pure generator differ byte-for-byte. Deriving the UUID from
    ``param_hash`` and ``tag`` (``"ref"``/``"mov"``) keeps it reproducible.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"stare-bench:{param_hash_value}:{tag}"))


def _warp(image, field):
    """Pull-sample ``image`` at ``xy + field(xy)`` -- the moving frame."""
    h, w = image.shape
    ys, xs = np.mgrid[0:h, 0:w]
    xy = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(float)
    src = xy + field.sample(xy)
    x = np.clip(src[:, 0], 0, w - 1)
    y = np.clip(src[:, 1], 0, h - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    tx, ty = x - x0, y - y0
    top = image[y0, x0] * (1 - tx) + image[y0, x1] * tx
    bot = image[y1, x0] * (1 - tx) + image[y1, x1] * tx
    return (top * (1 - ty) + bot * ty).reshape(h, w).astype(np.float32)


def _landmarks(shape, field, n, seed):
    """``(N, 4)`` array of ``[target_x, target_y, moving_x, moving_y]``.

    Emitted in the ANHIR ``,X,Y`` convention so
    ``registration_eval.eval_tre.evaluate_pair`` scores them unchanged.
    """
    h, w = shape
    rng = np.random.default_rng(seed + 991)
    margin = 0.05
    tx = rng.uniform(margin * w, (1 - margin) * w, n)
    ty = rng.uniform(margin * h, (1 - margin) * h, n)
    target = np.stack([tx, ty], axis=1)
    moving = target + field.sample(target)
    return np.concatenate([target, moving], axis=1)


def generate_pair(out_dir, size, seed, *, field_family="random_fourier",
                  field_params=None, physics_params=None, crop_source=None,
                  tile=2048, n_landmarks=200, write_dense_field=None,
                  lazy_images=False):
    """Generate one benchmark pair. Returns the truth dict it also writes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    field_params = dict(field_params or {})
    physics_params = dict(physics_params or {})
    crop_source = crop_source or SyntheticCropSource()
    h, w = int(size[0]), int(size[1])

    field = make_field(field_family, (h, w), seed=seed, **field_params)

    payload = {
        "generator_version": __version__,
        "seed": int(seed),
        "size": [h, w],
        "field_family": field_family,
        "field_params": field.params,
        "physics_params": physics_params,
        "crop_source": describe(crop_source),
        "tile": int(tile),
    }
    truth = dict(payload)
    truth["param_hash"] = param_hash(payload)
    truth["field_params_call"] = {"seed": int(seed), **field_params}

    if lazy_images:
        # Size-only path: used to assert the dense-field cap without paying for
        # a gigapixel render.
        truth["landmarks"] = []
        truth["tile_labels"] = []
        truth["dense_field_written"] = False
        (out_dir / "truth.json").write_text(json.dumps(truth, indent=2))
        return truth

    import tifffile

    ref = crop_source.crop((h, w), seed=seed)
    warped = _warp(ref, field)
    rng = np.random.default_rng(seed + 4242)
    mov, retained = apply_all(warped, physics_params, rng)

    ref_uuid = _stable_uuid(truth["param_hash"], "ref")
    mov_uuid = _stable_uuid(truth["param_hash"], "mov")
    tifffile.imwrite(out_dir / "ref.ome.tiff", ref, ome=True,
                     metadata={"UUID": f"urn:uuid:{ref_uuid}"})
    tifffile.imwrite(out_dir / "mov.ome.tiff", mov, ome=True,
                     metadata={"UUID": f"urn:uuid:{mov_uuid}"})

    lm = _landmarks((h, w), field, n_landmarks, seed)
    truth["landmarks"] = lm.tolist()
    truth["tile_labels"] = tile_labels(retained, (h, w), tile, field)

    if write_dense_field is None:
        write_dense_field = h * w <= DENSE_MAX_PIXELS
    if write_dense_field:
        np.save(out_dir / "field.npy", field.dense().astype(np.float32))
    truth["dense_field_written"] = bool(write_dense_field)

    (out_dir / "truth.json").write_text(json.dumps(truth, indent=2))
    return truth
