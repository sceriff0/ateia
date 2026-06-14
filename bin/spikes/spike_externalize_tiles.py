#!/usr/bin/env python3
"""THROWAWAY SPIKE (plan Task 1) — prove Strategy-2 externalize-and-resume is bit-identical.

Runs inside mirage-valis:1.0.0. Two independent proofs:

PART A/B — the make-or-break mechanism gate (fast, no JVM).
  Drive NonRigidTileRegistrar directly on two 128px grayscale test slides with a forced
  MULTI-tile grid (small tile_wh) so warp_tools.stitch_tiles' mblend seam is actually exercised.
    1. baseline: registrar.register(...) -> capture the stitched bk/fwd + dump per-tile inputs.
    2. recompute each tile in a SEPARATE OS process via VALIS's own reg_tile (subprocess).
    3. read-mode stitch (recomputed tiles -> stitch_tiles) must equal baseline EXACTLY, and each
       recomputed tile must equal the threaded loop's tile EXACTLY.
  Uses processing_cls=None (deterministic grayscale path) to isolate the externalization
  mechanism from the ChannelGetter bug documented below.

PART C — §5C halt -> pickle -> reload (full Valis, but halts BEFORE tile compute, so it never
  hits the ChannelGetter crash). Confirms the rigid-only registrar can be sanitized (VALIS's own
  cleanup()) and round-tripped through pickle into a fresh interpreter.

KNOWN BLOCKER discovered by this spike (record in spec §6.1):
  ChannelGetter.process_image (preprocessing.py:113) calls get_slide_reader(self.src_f)(self.src_f)
  UNCONDITIONALLY, before the `if self.image is None` check. NonRigidTileRegistrar.process_tile
  passes src_f=None, so VALIS 1.0.0's tiled non-rigid path crashes ("FileNotFoundError: 'None'")
  for ANY fluorescence/multichannel slide (ChannelGetter). Brightfield (ColorfulStandardizer) is
  fine. => classic auto-tiling is itself broken for fluorescence; the distributed path must feed
  the tiler pre-reduced single-channel images (or patch ChannelGetter) to tile fluorescence.

Usage (inside the image):
  docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 \
      python3 bin/spikes/spike_externalize_tiles.py all
"""
import argparse, os, sys, json, pickle, glob, threading, subprocess, traceback
import numpy as np

WORK = "/tmp/spike"
SRC_TESTDATA = "tests/testdata"
INPUT_DIR = "/tmp/spike_input"
REF = "P001_ref.ome.tiff"
MOV = "P001_mov1.ome.tiff"
SLIDES = ["P001_ref.ome.tiff", "P001_mov1.ome.tiff", "P001_mov2.ome.tiff"]
TILE_WH = 48        # small => multi-tile grid on the 128px slides (exercises stitch seam)
TILE_BUFFER = 16
SELF = os.path.abspath(__file__)


# --------------------------------------------------------------------------- helpers
def _vips_load(path):
    import pyvips
    return pyvips.Image.new_from_file(path)


def _to_np(field):
    from valis import warp_tools
    import pyvips
    if isinstance(field, pyvips.Image):
        arr = warp_tools.vips2numpy(field)
    elif isinstance(field, (list, tuple)):
        arr = np.dstack(field)
    else:
        arr = np.asarray(field)
    return np.ascontiguousarray(arr).astype(np.float32)


def _load_slide_gray(name):
    """Load a test slide as a single-band grayscale pyvips image suitable for the tiler."""
    import pyvips
    im = pyvips.Image.new_from_file(os.path.join(SRC_TESTDATA, name))
    if im.bands > 1:
        im = im.extract_band(0)
    return im


def _prep_input():
    import shutil
    os.makedirs(INPUT_DIR, exist_ok=True)
    for fn in os.listdir(INPUT_DIR):
        os.remove(os.path.join(INPUT_DIR, fn))
    for s in SLIDES:
        shutil.copy(os.path.join(SRC_TESTDATA, s), os.path.join(INPUT_DIR, s))
    print(f"[input] staged {len(SLIDES)} slides in {INPUT_DIR}", flush=True)


# --------------------------------------------------------------------------- PART A/B baseline
def mode_baseline():
    """Run NonRigidTileRegistrar.register() directly; capture stitched fields + dump tiler inputs
    + the threaded loop's real per-tile fields (ground truth)."""
    from valis import warp_tools
    from valis import non_rigid_registrars as nrr
    from valis.non_rigid_registrars import NonRigidTileRegistrar, OpticalFlowWarper

    os.makedirs(WORK, exist_ok=True)
    d = os.path.join(WORK, "tiler_0")
    os.makedirs(os.path.join(d, "real_tiles"), exist_ok=True)

    moving = _load_slide_gray(MOV)
    fixed = _load_slide_gray(REF)
    print(f"[baseline] moving {moving.width}x{moving.height}b{moving.bands} "
          f"fixed {fixed.width}x{fixed.height}b{fixed.bands} tile_wh={TILE_WH} buffer={TILE_BUFFER}",
          flush=True)

    holder = {}
    orig_calc = NonRigidTileRegistrar.calc

    def capture_calc(self, *a, **k):
        bk, fwd = orig_calc(self, *a, **k)
        holder["bk"], holder["fwd"] = bk, fwd
        holder["self"] = self
        return bk, fwd

    nrr.NonRigidTileRegistrar.calc = capture_calc
    reg = NonRigidTileRegistrar(tile_wh=TILE_WH, tile_buffer=TILE_BUFFER)
    reg.register(moving, fixed, mask=None,
                 non_rigid_registrar_cls=OpticalFlowWarper,
                 processing_cls=None, processing_kwargs=None, target_stats=None)
    nrr.NonRigidTileRegistrar.calc = orig_calc

    s = holder["self"]
    n = int(s.n_tiles)
    print(f"[baseline] n_tiles={n} grid={s.n_rows}x{s.n_cols}", flush=True)
    assert n >= 4, f"need a multi-tile grid to exercise the stitch seam; got {n}"

    np.save(os.path.join(d, "baseline_bk.npy"), _to_np(holder["bk"]))
    np.save(os.path.join(d, "baseline_fwd.npy"), _to_np(holder["fwd"]))
    s.moving_img.write_to_file(os.path.join(d, "moving.v"))
    s.fixed_img.write_to_file(os.path.join(d, "fixed.v"))
    np.save(os.path.join(d, "expanded_bboxes.npy"), np.asarray(s.expanded_bboxes))
    with open(os.path.join(d, "config.pkl"), "wb") as f:
        pickle.dump({
            "tile_wh": int(s.tile_wh), "tile_buffer": int(s.tile_buffer),
            "n_tiles": n, "n_rows": int(s.n_rows), "n_cols": int(s.n_cols),
            "has_mask": s.mask is not None,
            "has_target_stats": getattr(s, "target_stats", None) is not None,
            "processing_cls": s.processing_cls,
            "processing_kwargs": s.processing_kwargs,
            "non_rigid_registrar_cls": s.non_rigid_registrar_cls,
        }, f)
    for i in range(n):
        s.bk_dxdy_tiles[i].write_to_file(os.path.join(d, "real_tiles", f"bk_{i}.v"))
        s.fwd_dxdy_tiles[i].write_to_file(os.path.join(d, "real_tiles", f"fwd_{i}.v"))
    print(f"[baseline] dumped {n} tiles + inputs under {d}", flush=True)
    return n


# --------------------------------------------------------------------------- tile (fresh proc)
def mode_tile(dump_dir, tile_idx):
    from valis.non_rigid_registrars import NonRigidTileRegistrar
    try:
        from tqdm import tqdm
    except Exception:
        tqdm = None

    cfg = pickle.load(open(os.path.join(dump_dir, "config.pkl"), "rb"))
    reg = NonRigidTileRegistrar(tile_wh=cfg["tile_wh"], tile_buffer=cfg["tile_buffer"])
    reg.moving_img = _vips_load(os.path.join(dump_dir, "moving.v"))
    reg.fixed_img = _vips_load(os.path.join(dump_dir, "fixed.v"))
    reg.mask = _vips_load(os.path.join(dump_dir, "mask.v")) if cfg["has_mask"] else None
    reg.expanded_bboxes = np.load(os.path.join(dump_dir, "expanded_bboxes.npy"))
    reg.n_tiles = cfg["n_tiles"]; reg.n_rows = cfg["n_rows"]; reg.n_cols = cfg["n_cols"]
    reg.bk_dxdy_tiles = [None] * reg.n_tiles
    reg.fwd_dxdy_tiles = [None] * reg.n_tiles
    reg.non_rigid_registrar_cls = cfg["non_rigid_registrar_cls"]
    reg.processing_cls = cfg["processing_cls"]
    reg.processing_kwargs = cfg["processing_kwargs"]
    reg.target_stats = (np.load(os.path.join(dump_dir, "target_stats.npy"))
                        if cfg["has_target_stats"] else None)
    reg.pbar = tqdm(total=1) if tqdm else _NullPbar()

    reg.reg_tile(int(tile_idx), threading.Lock())

    out = os.path.join(dump_dir, "recomputed_tiles")
    os.makedirs(out, exist_ok=True)
    reg.bk_dxdy_tiles[int(tile_idx)].write_to_file(os.path.join(out, f"bk_{tile_idx}.v"))
    reg.fwd_dxdy_tiles[int(tile_idx)].write_to_file(os.path.join(out, f"fwd_{tile_idx}.v"))
    print(f"[tile] idx={tile_idx} OK (pid={os.getpid()})", flush=True)


class _NullPbar:
    def update(self, n): pass


# --------------------------------------------------------------------------- stitch_check
def mode_stitch_check(dump_dir):
    from valis import warp_tools
    cfg = pickle.load(open(os.path.join(dump_dir, "config.pkl"), "rb"))
    n = cfg["n_tiles"]
    expanded = np.load(os.path.join(dump_dir, "expanded_bboxes.npy"))

    per_tile_ok, max_tile_delta = True, 0.0
    for i in range(n):
        rc = _to_np(_vips_load(os.path.join(dump_dir, "recomputed_tiles", f"bk_{i}.v")))
        rl = _to_np(_vips_load(os.path.join(dump_dir, "real_tiles", f"bk_{i}.v")))
        if rc.shape != rl.shape:
            per_tile_ok = False
            print(f"[check] tile {i} SHAPE mismatch {rc.shape} vs {rl.shape}", flush=True); continue
        d = float(np.max(np.abs(rc - rl))) if rc.size else 0.0
        max_tile_delta = max(max_tile_delta, d)
        if not np.array_equal(rc, rl):
            per_tile_ok = False
            print(f"[check] tile {i} differs: max|delta|={d}", flush=True)

    bk_tiles = [_vips_load(os.path.join(dump_dir, "recomputed_tiles", f"bk_{i}.v")) for i in range(n)]
    fwd_tiles = [_vips_load(os.path.join(dump_dir, "recomputed_tiles", f"fwd_{i}.v")) for i in range(n)]
    stitched_bk = _to_np(warp_tools.stitch_tiles(bk_tiles, expanded, cfg["n_rows"], cfg["n_cols"], cfg["tile_buffer"]))
    stitched_fwd = _to_np(warp_tools.stitch_tiles(fwd_tiles, expanded, cfg["n_rows"], cfg["n_cols"], cfg["tile_buffer"]))
    base_bk = np.load(os.path.join(dump_dir, "baseline_bk.npy"))
    base_fwd = np.load(os.path.join(dump_dir, "baseline_fwd.npy"))

    bk_equal = stitched_bk.shape == base_bk.shape and np.array_equal(stitched_bk, base_bk)
    fwd_equal = stitched_fwd.shape == base_fwd.shape and np.array_equal(stitched_fwd, base_fwd)
    bk_delta = float(np.max(np.abs(stitched_bk - base_bk))) if stitched_bk.shape == base_bk.shape else float("nan")
    fwd_delta = float(np.max(np.abs(stitched_fwd - base_fwd))) if stitched_fwd.shape == base_fwd.shape else float("nan")

    print(f"[check] per_tile_ok={per_tile_ok} max_tile_delta={max_tile_delta} "
          f"| stitch_bk_equal={bk_equal} (max|delta|={bk_delta}) "
          f"stitch_fwd_equal={fwd_equal} (max|delta|={fwd_delta})", flush=True)
    return per_tile_ok and bk_equal and fwd_equal


# --------------------------------------------------------------------------- PART C halt/pickle
def mode_halt_pickle():
    from valis import registration
    from valis import non_rigid_registrars as nrr
    from valis.non_rigid_registrars import OpticalFlowWarper

    registration.TILER_THRESH_GB = 0
    os.makedirs(WORK, exist_ok=True)
    _prep_input()

    class TilesPending(Exception):
        pass

    orig_calc = nrr.NonRigidTileRegistrar.calc

    def halt_calc(self, *a, **k):
        raise TilesPending("halt at non-rigid boundary")

    nrr.NonRigidTileRegistrar.calc = halt_calc
    reg = registration.Valis(INPUT_DIR, os.path.join(WORK, "valis_halt_out"),
                             reference_img_f=REF, non_rigid_registrar_cls=OpticalFlowWarper,
                             max_non_rigid_registration_dim_px=512)
    halted = False
    try:
        reg.register()
    except TilesPending:
        halted = True
    except Exception as e:
        if "TilesPending" in repr(e) or "halt at non-rigid boundary" in str(e):
            halted = True
        else:
            print(f"[halt] register() raised non-halt error: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
    finally:
        nrr.NonRigidTileRegistrar.calc = orig_calc

    print(f"[halt] reached non-rigid boundary and halted: {halted}", flush=True)

    pkl_path = os.path.join(WORK, "halted_registrar.pkl")
    pickle_ok, pickle_err = False, None
    try:
        reg.cleanup()  # VALIS's own pre-pickle sanitization (nulls feature_detector etc.)
        print("[halt] called reg.cleanup()", flush=True)
    except Exception as e:
        print(f"[halt] cleanup() raised (continuing): {type(e).__name__}: {e}", flush=True)
    try:
        with open(pkl_path, "wb") as f:
            pickle.dump(reg, f)
        pickle_ok = True
        print(f"[halt] pickled Valis -> {pkl_path} ({os.path.getsize(pkl_path)/1e6:.1f} MB)", flush=True)
    except Exception as e:
        pickle_err = f"{type(e).__name__}: {e}"
        print(f"[halt] PICKLE FAILED: {pickle_err}", flush=True)

    reload_ok = False
    if pickle_ok:
        r = subprocess.run([sys.executable, SELF, "reload_check", "--pkl", pkl_path],
                           capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
        reload_ok = (r.returncode == 0)
    return halted, pickle_ok, reload_ok, pickle_err


def mode_reload_check(pkl_path):
    reg = pickle.load(open(pkl_path, "rb"))
    slide_dict = getattr(reg, "slide_dict", None)
    n_slides = len(slide_dict) if slide_dict else 0
    has_M = False
    if slide_dict:
        any_slide = next(iter(slide_dict.values()))
        has_M = getattr(any_slide, "M", None) is not None
    print(f"[reload] OK (pid={os.getpid()}): type={type(reg).__name__} n_slides={n_slides} "
          f"rigid_M_present={has_M}", flush=True)


def mode_pickle_roundtrip():
    """Does VALIS's OWN end-of-register registrar pickle reload in a fresh process?
    Runs register() to COMPLETION without forcing tiling (whole-image non-rigid -> ChannelGetter
    gets a real src_f, no crash). This isolates 'registrar pickle is fundamentally broken' from
    'only the mid-register halt point is unpicklable'."""
    from valis import registration
    from valis.non_rigid_registrars import OpticalFlowWarper

    registration.TILER_THRESH_GB = 10  # default: tiny data does NOT tile -> register completes
    os.makedirs(WORK, exist_ok=True)
    _prep_input()
    out = os.path.join(WORK, "valis_complete_out")
    reg = registration.Valis(INPUT_DIR, out, reference_img_f=REF,
                             non_rigid_registrar_cls=OpticalFlowWarper,
                             max_non_rigid_registration_dim_px=512)
    try:
        reg.register()
    except Exception as e:
        print(f"[roundtrip] register() raised: {type(e).__name__}: {e}", flush=True)
    # VALIS writes <name>_registrar.pickle into <out>/data during register()
    pickles = glob.glob(os.path.join(out, "**", "*_registrar.pickle"), recursive=True)
    print(f"[roundtrip] VALIS-written registrar pickles: {pickles}", flush=True)
    valis_pickle_ok = bool(pickles)
    reload_ok = False
    if pickles:
        r = subprocess.run([sys.executable, SELF, "reload_check", "--pkl", pickles[0]],
                           capture_output=True, text=True)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
        reload_ok = (r.returncode == 0)
    print(f"[roundtrip] valis_wrote_pickle={valis_pickle_ok} reload_in_fresh_proc_ok={reload_ok}", flush=True)
    return valis_pickle_ok, reload_ok


# --------------------------------------------------------------------------- orchestrator
def mode_all():
    print("\n##### PART A/B — externalize mechanism (direct tiler) #####\n", flush=True)
    n = mode_baseline()
    d = os.path.join(WORK, "tiler_0")
    print(f"\n[all] recomputing {n} tiles in fresh processes ...\n", flush=True)
    for i in range(n):
        r = subprocess.run([sys.executable, SELF, "tile", "--dump-dir", d, "--tile-idx", str(i)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
            raise SystemExit(f"[all] tile #{i} failed")
    ab_ok = mode_stitch_check(d)

    print("\n##### PART C — §5C halt -> cleanup -> pickle -> reload #####\n", flush=True)
    try:
        halted, pickle_ok, reload_ok, perr = mode_halt_pickle()
    except Exception as e:
        halted, pickle_ok, reload_ok, perr = False, False, False, f"{type(e).__name__}: {e}"
        traceback.print_exc()

    print("\n" + "=" * 72)
    print(f"PART A/B  BIT-IDENTICAL (recompute+stitch == baseline, {n} tiles): {ab_ok}")
    print(f"PART C    HALT/PICKLE/RESUME: halted={halted} pickle_ok={pickle_ok} reload_ok={reload_ok}"
          + (f" err={perr}" if perr else ""))
    print("=" * 72, flush=True)
    return ab_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["all", "baseline", "tile", "stitch_check",
                                     "halt_pickle", "reload_check", "pickle_roundtrip"])
    ap.add_argument("--dump-dir")
    ap.add_argument("--tile-idx")
    ap.add_argument("--pkl")
    args = ap.parse_args()

    if args.mode == "all":
        return 0 if mode_all() else 1
    if args.mode == "baseline":
        mode_baseline(); return 0
    if args.mode == "tile":
        mode_tile(args.dump_dir, args.tile_idx); return 0
    if args.mode == "stitch_check":
        return 0 if mode_stitch_check(args.dump_dir) else 1
    if args.mode == "halt_pickle":
        mode_halt_pickle(); return 0
    if args.mode == "reload_check":
        mode_reload_check(args.pkl); return 0
    if args.mode == "pickle_roundtrip":
        mode_pickle_roundtrip(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
