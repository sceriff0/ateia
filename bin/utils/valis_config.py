"""Single source of truth for the VALIS registrar configuration.

Both the classic `bin/register.py` and the distributed `bin/reg_prep.py` build their `Valis`
object from `build_registrar_kwargs(...)` here, so the distributed path's rigid stage (feature
detector, matcher, MicroRigidRegistrar, image-dim caps, affine optimizer) is **bit-identical** to
classic. Any drift here would change the rigid `M` and break bit-identicality.
"""
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


def micro_reg_size(slide_dict, micro_reg_fraction=0.125):
    """Replicate register.py's micro_reg_size = floor(min over slides of max(dim) * fraction)."""
    import numpy as np
    img_dims = np.array([s.slide_dimensions_wh[0] for s in slide_dict.values()])
    min_max_size = np.min([np.max(d) for d in img_dims])
    return int(np.floor(min_max_size * micro_reg_fraction))
