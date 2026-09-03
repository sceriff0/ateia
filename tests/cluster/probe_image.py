#!/usr/bin/env python3
"""Probe one converted slide and print a markdown table row.

Run INSIDE the convert container -- `.czi`, `.nd2`, `.lif` and `.svs` are
readable only with the plugin set baked into bolt3x/mirage-convert, and `.svs`
additionally needs its Bio-Formats jars (RULING R2). A login node's Python has
neither.

Lives under tests/, not bin/: tests/test_no_dead_bin_modules.py requires every
bin/utils module to have a production importer, and this has none by design.

Everything is read through bin/utils/ome_io.py, so this reports what the
PIPELINE sees, not what a second, differently-configured reader sees.

IMPORT CONVENTION. `ome_io` is a bin/utils module and imports its siblings
(`jvm_cache`, `pixel_size`, `tiled_io`) with a flat `from jvm_cache import ...`,
so both `bin` and `bin/utils` must be on `sys.path` -- matching
tests/integration/formats/test_ome_io_read_info.py, not a plain
`from utils import ome_io` (which imports cleanly but then dies inside ome_io.py
itself with `ModuleNotFoundError: No module named 'jvm_cache'`, since `bin/utils`
was never added to sys.path -- only `bin` was).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))
sys.path.insert(0, str(REPO_ROOT / "bin" / "utils"))

import ome_io  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Print one markdown row describing an image, via ome_io."
    )
    parser.add_argument("--image", required=True, help="Image to probe.")
    parser.add_argument(
        "--label",
        default=None,
        help="What to call this row (defaults to the file's suffix).",
    )
    return parser.parse_args()


def describe(path: Path, label: str) -> str:
    """Return one markdown table row for `path`, or a row recording the failure."""
    started = time.time()
    try:
        reader = ome_io.detect_reader(path)
        info = ome_io.read_info(path)
    except Exception as exc:  # noqa: BLE001 -- the failure IS the result here
        return (
            f"| {label} | `{path.name}` | — | — | — | — | — | "
            f"{time.time() - started:.1f} | FAILED: {type(exc).__name__}: {exc} |"
        )
    channels = ", ".join(info.channels) if info.channels else "—"
    pixel = "—" if info.pixel_size_um is None else f"{info.pixel_size_um:.4f}"
    return (
        f"| {label} | `{path.name}` | {reader} | "
        f"{info.shape_cyx[0]}x{info.shape_cyx[1]}x{info.shape_cyx[2]} | "
        f"{info.dtype} | {channels} | {pixel} | "
        f"{time.time() - started:.1f} | OK |"
    )


def main() -> int:
    """CLI entry point: print one markdown row for one image."""
    args = parse_args()
    path = Path(args.image)
    label = args.label or (path.suffix.lower() or "(no suffix)")
    print(describe(path, label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
