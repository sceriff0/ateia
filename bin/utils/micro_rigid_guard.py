"""Robustness guard for VALIS's MicroRigidRegistrar (rigid-stage micro alignment, enabled when
``skip_micro_registration=false``).

VALIS's ``MicroRigidRegistrar.align_slides`` does ``np.vstack(high_rez_match_list)``
(``micro_rigid_registrar.py:331``) with NO guard for the case where no high-resolution feature
matches are found in any tile — it raises ``ValueError: need at least one array to concatenate`` and
takes down the whole ``register()`` run. This happens on low-texture / featureless regions (and on
synthetic fixtures, which lack distinctive high-rez SuperPoint/SuperGlue features).

The vstack is reached BEFORE the slide's ``M`` is updated (M is computed at line 356+), so "no
high-rez matches" should simply mean "no micro-rigid adjustment" (leave the rigid ``M`` as-is) — not a
crash. This guard wraps ``align_slides`` to do exactly that.

It is a **no-op on real data** (where high-rez matches exist, so the guard never triggers), so it does
not change registration output where the goal's bit-identicality applies; it only prevents the crash
on featureless input. Installed identically by the classic and distributed paths so any comparison
between them stays apples-to-apples.
"""


def install_micro_rigid_guard():
    """Idempotently wrap MicroRigidRegistrar.align_slides to skip (M unchanged) when no high-rez
    matches are found, instead of crashing on np.vstack([])."""
    from valis import valtils
    from valis.micro_rigid_registrar import MicroRigidRegistrar

    if getattr(MicroRigidRegistrar, "_empty_match_guard", False):
        return
    orig_align = MicroRigidRegistrar.align_slides

    def guarded_align(self, moving_slide, fixed_slide, *args, **kwargs):
        try:
            return orig_align(self, moving_slide, fixed_slide, *args, **kwargs)
        except ValueError as e:
            if "at least one array to concatenate" in str(e):
                valtils.print_warning(
                    f"MicroRigidRegistrar: no high-rez matches for {moving_slide.name}->"
                    f"{fixed_slide.name}; skipping micro-rigid adjustment (rigid M unchanged). "
                    f"This is a no-op on feature-rich (real) data."
                )
                return None
            raise

    MicroRigidRegistrar.align_slides = guarded_align
    MicroRigidRegistrar._empty_match_guard = True
