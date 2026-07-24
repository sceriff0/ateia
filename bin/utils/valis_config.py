"""Single source of truth for the VALIS registrar configuration.

`bin/register.py` builds its `Valis` object from `build_registrar_kwargs(...)` here, so the
registrar's feature detector, matcher, MicroRigidRegistrar, image-dim caps and affine optimizer
have a single definition. `init_jvm(...)` is the shared BioFormats JVM-heap sizer, also used by
`bin/warp_seg_qc.py` for the reg_qc>=2 segmentation-overlap QC.
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


def init_jvm(input_dir, override_gb=None):
    """Size and start the BioFormats JVM heap to the inputs, mirroring bin/register.py:333-335.

    Used by bin/warp_seg_qc.py, which constructs a real ``Valis`` and reads slides via BioFormats.
    Heap formula is copied from register.py's estimate_jvm_memory (total*3+8, min 8, capped at
    75% of system RAM)."""
    from valis import registration

    # Point scyjava's jgo/Maven cache off a read-only $HOME (HPC nodes) BEFORE the JVM starts.
    # scyjava<1.11 derives the cache path from Path.home() and ignores JGO_CACHE_DIR/M2_REPO, so
    # the Dockerfile ENV knobs are inert; this uses scyjava.config.set_cache_dir instead. Without
    # it, jgo's os.makedirs($HOME/.jgo) dies with EROFS on /hpcnfs. See jvm_cache.py.
    from jvm_cache import point_jvm_cache_off_readonly_home
    point_jvm_cache_off_readonly_home()

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
