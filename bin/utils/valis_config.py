"""Single source of truth for the VALIS registrar configuration.

Both the classic `bin/register.py` and the distributed `bin/reg_prep.py` build their `Valis`
object from `build_registrar_kwargs(...)` here, so the distributed path's rigid stage (feature
detector, matcher, MicroRigidRegistrar, image-dim caps, affine optimizer) is **bit-identical** to
classic. Any drift here would change the rigid `M` and break bit-identicality.
"""
import os

from valis import feature_detectors, feature_matcher
from valis.micro_rigid_registrar import MicroRigidRegistrar
from valis.non_rigid_registrars import OpticalFlowWarper

# Memory mode presets — bundle feature detector, matcher, and dimension settings.
# (Kept identical to the historical register.py MEMORY_PRESETS.)
MEMORY_PRESETS = {
    "high": {
        "feature_detector_cls": feature_detectors.SuperPointFD,
        "matcher": feature_matcher.SuperGlueMatcher(),
        "max_processed_image_dim_px": 2048,
        "max_non_rigid_registration_dim_px": 4096,
        "num_features": 5000,
    },
    "medium": {
        "feature_detector_cls": feature_detectors.SuperPointFD,
        "matcher": feature_matcher.SuperGlueMatcher(),
        "max_processed_image_dim_px": 1024,
        "max_non_rigid_registration_dim_px": 4096,
        "num_features": 5000,
    },
    "low": {
        "feature_detector_cls": feature_detectors.SuperPointFD,
        "matcher": feature_matcher.SuperGlueMatcher(),
        "num_features": 5000,
        "max_processed_image_dim_px": 256,
        "max_non_rigid_registration_dim_px": 1024,
        "tile_wh": 512,
        "tile_buffer": 100,
    },
}


def build_registrar_kwargs(reference_img_f, memory_mode="high", skip_micro_registration=False,
                           max_image_dim_px=4000):
    """Return the exact kwargs dict passed to `registration.Valis(...)` by classic register.py.

    NOTE: a fresh `SuperGlueMatcher()` instance is created per call (mirrors register.py, which
    instantiates the matcher from the preset). The matcher carries no cross-run RNG state that
    affects determinism for our purposes (SuperPoint/SuperGlue inference is deterministic).
    """
    preset = MEMORY_PRESETS[memory_mode]
    return {
        "reference_img_f": reference_img_f,
        "align_to_reference": True,
        "crop": "reference",
        "max_processed_image_dim_px": preset["max_processed_image_dim_px"],
        "max_non_rigid_registration_dim_px": preset["max_non_rigid_registration_dim_px"],
        "max_image_dim_px": preset.get("max_image_dim_px", max_image_dim_px),
        "feature_detector_cls": preset["feature_detector_cls"],
        "matcher": preset["matcher"],
        "non_rigid_registrar_cls": OpticalFlowWarper,
        "affine_optimizer_cls": None,
        "micro_rigid_registrar_cls": None if skip_micro_registration else MicroRigidRegistrar,
        "create_masks": True,
    }


def _system_memory_gb():
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) // (1024 ** 3)
    except Exception:
        return None


def _redirect_jvm_home_off_readonly():
    """HPC read-only-$HOME fix for the BioFormats JVM (OSError [Errno 30] Read-only file system).

    The patched distributed container pins ``scyjava<1.11``, which starts the JVM via ``jgo``. The
    first ``scyjava.start_jvm()`` makes ``jgo`` build a Maven workspace under ``$HOME/.jgo`` — and on
    IEO compute nodes ``$HOME`` (``/hpcnfs/...``) is read-only, so ``os.makedirs()`` dies with EROFS
    before registration can start. This is the successor to the ``cjdk`` crash the old
    ``bin/utils/hpc_scratch.py`` guarded (retired by the scyjava<1.11 pin): ``jgo``'s workspace is
    ``$HOME``-derived, so ``XDG_CACHE_HOME`` alone (set inline in the entrypoints) does NOT cover it.

    Only redirect when ``$HOME`` is actually unwritable, so local/dev runs and any image that bakes its
    jgo/Maven cache under a writable ``$HOME`` are left untouched. Must run BEFORE ``start_jvm()``.

    Preference order when $HOME is read-only:
      1. A WARM jgo cache baked into the image. The container is built as root, so ``/root/.jgo``
         already holds the resolved BioFormats workspace — pointing HOME there means jgo needs no
         writes and (critically) no network, which matters because compute nodes are usually offline.
         Override the search path with ``$MIRAGE_JVM_HOME`` if the image uses a different build home.
      2. Node-local writable scratch (``$TMPDIR``, which SLURM/Nextflow set; else ``/tmp``). jgo will
         (re)build its workspace here — this needs the BioFormats jars reachable via ``~/.m2`` or the
         network, so it only helps if the jars are bundled offline."""
    home = os.path.expanduser("~")
    if os.access(home, os.W_OK):
        return  # writable $HOME (local/dev, or a baked cache) — leave everything as-is

    # 1) prefer a warm baked cache (no writes, no network)
    for baked in (os.environ.get("MIRAGE_JVM_HOME", ""), "/root"):
        if baked and os.path.isdir(os.path.join(baked, ".jgo")):
            os.environ["HOME"] = baked
            return

    # 2) fall back to node-local writable scratch
    scratch = os.environ.get("TMPDIR") or "/tmp"
    new_home = os.path.join(scratch, "mirage_jvm_home")
    for sub in (".jgo", ".m2", ".cache"):
        os.makedirs(os.path.join(new_home, sub), exist_ok=True)
    os.environ["HOME"] = new_home
    os.environ.setdefault("JGO_CACHE_DIR", os.path.join(new_home, ".jgo"))
    os.environ.setdefault("XDG_CACHE_HOME", os.path.join(new_home, ".cache"))


def init_jvm(input_dir, override_gb=None):
    """Size and start the BioFormats JVM heap to the inputs, mirroring bin/register.py:333-335.

    The distributed prep stages (reg_prep, reg_micro_prep) construct a real ``Valis`` and read slides
    via BioFormats, exactly like classic register.py — so they MUST init the JVM with a heap sized to
    the inputs. Without this the JVM either never starts, or starts with VALIS's small default heap and
    OOMs reading a large slide on a cluster node — the RAM-on-one-node failure the distributed path
    exists to remove. Heap formula is copied from register.py's estimate_jvm_memory (total*3+8, min 8,
    capped at 75% of system RAM)."""
    from valis import registration

    # Point $HOME-based JVM caches (jgo Maven workspace) at writable scratch before the JVM starts.
    _redirect_jvm_home_off_readonly()

    if override_gb is not None and override_gb > 0:
        mem_gb = int(override_gb)
    else:
        total_gb = 0.0
        for f in os.listdir(input_dir):
            if f.lower().endswith((".tif", ".tiff", ".ome.tif", ".ome.tiff")):
                total_gb += os.path.getsize(os.path.join(input_dir, f)) / (1024 ** 3)
        sys_mem = _system_memory_gb()
        max_heap = int(sys_mem * 0.75) if sys_mem else 64
        mem_gb = max(8, min(max_heap, int(total_gb * 3 + 8)))
    registration.init_jvm(mem_gb=mem_gb)
    return mem_gb


def micro_reg_size(slide_dict, micro_reg_fraction=0.125):
    """Replicate register.py's micro_reg_size = floor(min over slides of max(dim) * fraction)."""
    import numpy as np
    img_dims = np.array([s.slide_dimensions_wh[0] for s in slide_dict.values()])
    min_max_size = np.min([np.max(d) for d in img_dims])
    return int(np.floor(min_max_size * micro_reg_fraction))
