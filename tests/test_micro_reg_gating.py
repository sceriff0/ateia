"""Micro-registration ordinal gating.

Two independent VALIS micro passes are gated by the single ``micro_reg`` ordinal:
  * ``MicroRigidRegistrar`` (refines ``slide.M`` inside ``Valis.register()``) — enabled at level >= 1,
    wired via ``build_registrar_kwargs``'s ``micro_rigid_registrar_cls`` kwarg.
  * ``register_micro()`` (the non-rigid micro pass) — a separate call in ``register.py`` gated at
    level >= 2.

valis is not installed in CI, so the valis modules ``bin/utils/valis_config.py`` imports are faked
in ``sys.modules`` before importing it (same approach as ``test_jvm_cache_guard.py``). The
register.py level-2 gate is asserted at the source level (no valis import needed).
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

UTILS = Path(__file__).resolve().parent.parent / "bin" / "utils"
if str(UTILS) not in sys.path:
    sys.path.insert(0, str(UTILS))

REGISTER_PY = Path(__file__).resolve().parent.parent / "bin" / "register.py"


class _MicroRigidSentinel:
    """Stand-in for valis's MicroRigidRegistrar so we can assert identity of the wired class."""


@pytest.fixture
def valis_config(monkeypatch):
    """Import bin/utils/valis_config with the valis package faked out."""
    micro_mod = types.ModuleType("valis.micro_rigid_registrar")
    micro_mod.MicroRigidRegistrar = _MicroRigidSentinel
    nonrigid_mod = types.ModuleType("valis.non_rigid_registrars")
    nonrigid_mod.OpticalFlowWarper = type("OpticalFlowWarper", (), {})
    # feature detector / matcher classes are looked up as attributes by MEMORY_PRESETS;
    # MagicMock auto-provides whatever attribute names the presets reference.
    fd_mod = MagicMock(name="valis.feature_detectors")
    fm_mod = MagicMock(name="valis.feature_matcher")

    valis_pkg = types.ModuleType("valis")
    valis_pkg.feature_detectors = fd_mod
    valis_pkg.feature_matcher = fm_mod
    valis_pkg.micro_rigid_registrar = micro_mod
    valis_pkg.non_rigid_registrars = nonrigid_mod

    for name, mod in {
        "valis": valis_pkg,
        "valis.feature_detectors": fd_mod,
        "valis.feature_matcher": fm_mod,
        "valis.micro_rigid_registrar": micro_mod,
        "valis.non_rigid_registrars": nonrigid_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # Force a fresh import so the fakes are picked up (in case an earlier test imported it).
    monkeypatch.delitem(sys.modules, "valis_config", raising=False)
    import valis_config as vc

    return vc


def test_micro_rigid_disabled_at_level_0(valis_config):
    kw = valis_config.build_registrar_kwargs("ref.ome.tiff", micro_reg=0)
    assert kw["micro_rigid_registrar_cls"] is None


@pytest.mark.parametrize("level", [1, 2])
def test_micro_rigid_enabled_at_levels_1_and_2(valis_config, level):
    kw = valis_config.build_registrar_kwargs("ref.ome.tiff", micro_reg=level)
    # nesting: level 1 and 2 both enable the micro-rigid pass (0 ⊂ 1 ⊂ 2).
    assert kw["micro_rigid_registrar_cls"] is _MicroRigidSentinel


def test_register_py_gates_register_micro_at_level_2():
    """Source-level canary (valis-free): the register_micro() non-rigid pass and the honest
    checkpoint flag must stay gated at micro_reg >= 2. Fails loudly if the gate is removed."""
    src = REGISTER_PY.read_text()
    assert "register_micro" in src
    assert "micro_reg < 2" in src, (
        "register_micro() must stay gated (skipped at micro_reg < 2)"
    )
    assert "micro_registration=(micro_reg >= 2)" in src, (
        "stage checkpoint must record the honest micro flag (micro_reg >= 2)"
    )
