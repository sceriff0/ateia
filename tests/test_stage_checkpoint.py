"""Tests for bin/utils/stage_checkpoint.py — the pre-micro snapshot REGISTER takes so that
WARP_SEG_QC can tell the non_rigid stage from the micro stage.

The contract this file defends: the snapshot round-trips, it survives a slide that cannot be
snapshotted (registration must never fail because a QC artifact could not be written), and it
refuses to be read by a QC that expects a different layout.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"))

import stage_checkpoint as sc


class _Slide:
    def __init__(self, M=None, fwd_dxdy=None):
        self.M = M
        self.fwd_dxdy = fwd_dxdy


class _Registrar:
    def __init__(self, slide_dict):
        self.slide_dict = slide_dict


def _field(h=4, w=5, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(size=(2, h, w)).astype(np.float32)


# ── write / read round trip ────────────────────────────────────────────────────
def test_round_trip_returns_the_same_field(tmp_path):
    f = _field()
    reg = _Registrar({"ref": _Slide(M=np.eye(3)), "mov": _Slide(M=np.eye(3), fwd_dxdy=f)})
    sc.write_checkpoint(reg, str(tmp_path))

    ck = sc.StageCheckpoint.load(str(tmp_path))
    assert ck.micro_registration is True
    assert sorted(ck.slide_names) == ["mov", "ref"]
    assert np.allclose(ck.fwd_dxdy("mov"), f)


def test_a_slide_without_a_field_reads_back_as_none(tmp_path):
    """The reference has no displacement field — register_micro skips it — and 'no field'
    is the correct answer for it, not an error."""
    reg = _Registrar({"ref": _Slide(M=np.eye(3)), "mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path))
    ck = sc.StageCheckpoint.load(str(tmp_path))
    assert ck.fwd_dxdy("ref") is None
    assert ck.has_slide("ref")


def test_manifest_records_M_and_field_shape_for_audit(tmp_path):
    f = _field(h=3, w=7)
    reg = _Registrar({"mov": _Slide(M=np.eye(3) * 2, fwd_dxdy=f)})
    manifest = sc.write_checkpoint(reg, str(tmp_path))
    assert manifest["slides"]["mov"]["field_shape"] == [2, 3, 7]
    assert manifest["slides"]["mov"]["M"] == (np.eye(3) * 2).tolist()
    assert manifest["stage"] == "post_non_rigid_pre_micro"
    assert manifest["errors"] == []


def test_load_accepts_the_manifest_file_as_well_as_the_directory(tmp_path):
    reg = _Registrar({"mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path))
    from_dir = sc.StageCheckpoint.load(str(tmp_path))
    from_file = sc.StageCheckpoint.load(str(tmp_path / sc.MANIFEST_NAME))
    assert np.allclose(from_dir.fwd_dxdy("mov"), from_file.fwd_dxdy("mov"))


def test_slide_names_with_awkward_characters_get_safe_filenames(tmp_path):
    name = "patient 1/cycle:2"
    reg = _Registrar({name: _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path))
    ck = sc.StageCheckpoint.load(str(tmp_path))
    assert ck.has_slide(name)              # the key keeps the original name...
    assert np.isfinite(ck.fwd_dxdy(name)).all()   # ...and the file is still readable


# ── the micro_registration flag ────────────────────────────────────────────────
def test_micro_registration_false_is_recorded(tmp_path):
    reg = _Registrar({"mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path), micro_registration=False)
    assert sc.StageCheckpoint.load(str(tmp_path)).micro_registration is False


def test_set_micro_registration_corrects_the_flag_after_the_fact(tmp_path):
    """VALIS's micro stage is caught-and-continued, so whether it ran is known only later."""
    reg = _Registrar({"mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path), micro_registration=True)
    assert sc.set_micro_registration(str(tmp_path), False) is True
    assert sc.StageCheckpoint.load(str(tmp_path)).micro_registration is False


def test_set_micro_registration_on_a_missing_checkpoint_reports_failure_not_raises(tmp_path):
    assert sc.set_micro_registration(str(tmp_path / "nope"), True) is False


# ── failure containment ────────────────────────────────────────────────────────
def test_a_slide_that_cannot_be_snapshotted_is_recorded_not_raised(tmp_path):
    """Registration must never fail because a QC artifact could not be written."""
    bad = _Slide(M=np.eye(3), fwd_dxdy=np.zeros((3, 4, 5)))   # not a (2, H, W) field
    good = _Slide(M=np.eye(3), fwd_dxdy=_field())
    manifest = sc.write_checkpoint(_Registrar({"bad": bad, "good": good}), str(tmp_path))

    assert len(manifest["errors"]) == 1
    assert "bad" in manifest["errors"][0]
    ck = sc.StageCheckpoint.load(str(tmp_path))
    assert ck.fwd_dxdy("bad") is None            # recorded as fieldless, not corrupt
    assert np.allclose(ck.fwd_dxdy("good"), good.fwd_dxdy)   # the good slide survived


def test_asking_for_an_unknown_slide_names_the_ones_it_has(tmp_path):
    reg = _Registrar({"mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path))
    ck = sc.StageCheckpoint.load(str(tmp_path))
    with pytest.raises(KeyError, match="mov"):
        ck.fwd_dxdy("some_other_slide")


def test_a_checkpoint_from_a_different_version_is_refused(tmp_path):
    reg = _Registrar({"mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(tmp_path))
    manifest_f = tmp_path / sc.MANIFEST_NAME
    manifest = json.loads(manifest_f.read_text())
    manifest["version"] = sc.CHECKPOINT_VERSION + 1
    manifest_f.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="version"):
        sc.StageCheckpoint.load(str(tmp_path))


def test_write_checkpoint_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "checkpoint"
    reg = _Registrar({"mov": _Slide(M=np.eye(3), fwd_dxdy=_field())})
    sc.write_checkpoint(reg, str(target))
    assert os.path.isfile(target / sc.MANIFEST_NAME)
