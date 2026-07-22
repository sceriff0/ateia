#!/usr/bin/env python3
"""Verify the low-memory registration path is bit-identical to what it replaces.

Leg 1 (this task): reg_finalize with the lazy pyvips reader == reg_finalize with BioFormats.
Leg 2 (Task 4):    tile fan-out + assemble == the single-process warp.

Usage (inside the VALIS image):
  docker run --rm -v "$PWD":/work -w /work \
      bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/verify_lowmem_bitidentical.py
"""
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pyvips

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"))

WORK = "/tmp/verify_lowmem"
INP = os.path.join(WORK, "in")
PREP = os.path.join(WORK, "prep")
REF, MOV = "P001_ref.ome.tiff", "P001_mov1.ome.tiff"
MOV_STEM = "P001_mov1"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit(f"FAILED: {' '.join(cmd[1:4])}")
    return r


def px(path):
    from valis import warp_tools
    return warp_tools.vips2numpy(pyvips.Image.new_from_file(path)).astype(np.float64)


def setup():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        shutil.copy(os.path.join("tests/testdata", s), os.path.join(INP, s))
    run([sys.executable, "bin/reg_prep.py", "--input-dir", INP, "--out", PREP,
         "--reference", REF, "--memory-mode", "low", "--skip-micro-registration"])
    return os.path.join(PREP, MOV_STEM)


def leg1(md):
    """Lazy reader vs BioFormats, same field, same warp state."""
    ti = os.path.join(md, "tiler_inputs")
    ws = os.path.join(md, "warp_state.json")
    field = os.path.join(md, "nr", "bk.v")
    os.makedirs(os.path.join(md, "nr"), exist_ok=True)
    run([sys.executable, "bin/reg_nonrigid.py", "--inputs-dir", ti,
         "--out-dir", os.path.join(md, "nr")])

    lazy_out = os.path.join(md, "lazy.ome.tiff")
    bf_out = os.path.join(md, "bf.ome.tiff")
    run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--field", field,
         "--warp-state", ws, "--src-slide", os.path.join(INP, MOV), "--out", lazy_out])
    env = dict(os.environ, MIRAGE_FORCE_BIOFORMATS="1")
    r = subprocess.run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--field", field,
                        "--warp-state", ws, "--src-slide", os.path.join(INP, MOV), "--out", bf_out],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit("FAILED: BioFormats reference run")

    a, b = px(lazy_out), px(bf_out)
    equal = a.shape == b.shape and np.array_equal(a, b)
    d = None if a.shape != b.shape else float(np.max(np.abs(a - b)))
    print("=" * 72)
    print(f"LEG 1 lazy-reader vs BioFormats: equal={equal} max|delta|={d}")
    print("=" * 72, flush=True)
    return 0 if equal else 1


def main():
    md = setup()
    return leg1(md)


if __name__ == "__main__":
    raise SystemExit(main())
