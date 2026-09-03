"""The container images are one dependency surface, and this file is where it is declared.

Ten images build the pipeline. Before harmonisation they disagreed with each other and, in three
places, with themselves:

  * ``containers/merge`` installed a bare ``zarr``. A rebuild picks up zarr 3, where the
    ``zarr.hierarchy`` module is REMOVED (zarr's own 3.0 migration guide lists it). The one
    consumer wraps the call in ``except Exception`` and falls back to a whole-array ``imread``,
    so the failure is not a crash -- it is the lazy read silently turning into a full
    materialisation of the largest artifact in the pipeline.
  * ``containers/segmentation/requirements.txt`` was never ``COPY``d and never installed. It is
    a byte-identical copy of ``containers/debug_diffeo/requirements.txt`` (md5 792dc481...), so
    its ``tifffile==2023.4.12`` line read like a pin while pinning nothing.
  * ``containers/quantification`` -- the image behind seven processes, more than any other --
    pinned none of its nine packages.

The rule that catches the worst class of these is ``test_container_installs_what_its_scripts_import``:
``bin/convert_image.py`` has imported ``bioio`` since 2026-01-05, and no container in the repo has
ever installed it, including after the bioformats image was edited five months later. A container
that does not install what its own scripts import is broken at runtime, not at build time.

Version ceilings are set by the base images' Python, not by what is newest on PyPI. Seven of the
ten bases are Python 3.10 (``ubuntu:22.04``, ``*-jammy``, ``cuda-*-ubuntu22.04``), and zarr 3,
tifffile 2026.x and scipy 1.18 all require >=3.12. ``containers/spatialdata`` is a deliberate
exception: it is ``python:3.12-slim``, it is the only image whose dependency (spatialdata>=0.8.0)
*requires* zarr>=3, and it shares no zarr-touching code with the rest.
"""

import ast
import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement

from tests.ci_actions import strip_line_comment
from tests.nfmodel import processes, strip_comments

REPO = Path(__file__).resolve().parent.parent
CONTAINERS = REPO / "containers"
REQUIREMENTS = REPO / "requirements"
CONSTRAINTS = REQUIREMENTS / "constraints.txt"

# Since the one-repo-per-image rename, the component name in the image reference IS the
# build-context directory name, so no translation table is needed. The previous table mapped
# ten hand-written tags onto differently-spelled directories, which is precisely how
# `instant_seg` and `istantseg` came to misspell InstanSeg two different ways.

# The one image allowed to diverge, and why. It is python:3.12-slim and spatialdata>=0.8.0
# requires zarr>=3, which cannot be installed on the python:3.10 bases the others use.
PY312_ISLAND = "spatialdata"

# Packages installed by more than one image must agree on a version.
#
# THIS TABLE IS NOT WRITTEN HERE ANY MORE. It is READ from requirements/constraints.txt --
# the very file the Dockerfiles COPY and pass to pip with `-c`, and the one CI's requirements
# files constrain against. It used to be a hand-maintained dict beside a hand-maintained copy
# of the same numbers in the workflow YAML, with a 42 KB guard test proving the two copies
# equal. Reading the file deletes the second copy instead of checking it: an image can no
# longer disagree with "the harmonised set" while the harmonised set agrees with nothing that
# actually gets installed.
#
# Ceilings are the newest release that still supports Python 3.10, except numpy, which is held
# at the last 1.x because the segmentation image's TensorFlow 2.15 base requires numpy<2. The
# rationale for each pin, and for the four packages deliberately NOT constrained, lives in
# requirements/constraints.txt itself.


def _read_constraints(path=CONSTRAINTS):
    """{name: version} from a pip constraints file. Exact pins only, which is all it may hold.

    A constraints file may legally carry inexact specifiers; this one may not, and saying so
    HERE is what keeps `>=`-style drift out of the harmonised set. Comment-only lines and the
    documented "not constrained here" block are skipped by the `#` strip, so the block that
    records matplotlib/scikit-learn/opencv/torch's authorities cannot accidentally become a
    pin.
    """
    out = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        parsed = _parse_req(line)
        assert parsed, f"{path.name}: cannot parse requirement {line!r}"
        name, specs = parsed
        assert len(specs) == 1 and specs[0][0] == "==", (
            f"{path.name} pins {name!r} as {line!r}. Every line in the constraints file must "
            f"be an exact `name==version`: it is the single version authority for every "
            f"container image and every CI job, and an inexact one authorises drift."
        )
        out[name] = specs[0][1]
    assert out, (
        f"{path} parsed to an EMPTY table. Every harmonised check below would then pass "
        f"having compared nothing -- the vacuous-guard failure this repo keeps hitting."
    )
    return out


# Documented per-image exceptions to HARMONISED. Each names the UPSTREAM CONSTRAINT that forces
# it, because an exception without one is just a silent escape hatch from the harmonised set.
#
# scipy used to be the case that proved the rule: harmonising it to 1.15.3 everywhere made the
# preprocess image unbuildable, since basicpy 1.2.0 -- the in-process illumination-correction
# dependency -- caps scipy below 1.13. That entry is GONE, and its removal is what the exception
# mechanism is for. Illumination correction now runs through nf-core's BASICPY module, in
# labsyspharm's own container; `containers/preprocess` no longer installs basicpy at all, so
# nothing caps its scipy and it is back on the harmonised 1.15.3.
# test_every_pinned_exception_is_still_doing_something is what forced the issue: leaving the
# entry behind would have failed, because the Dockerfile it named no longer diverges.
PINNED_EXCEPTIONS = {
    # bioio-ome-tiff 1.4.0 requires tifffile[zarr]<2025.1.10 on python_version < "3.11"; the
    # convert base (eclipse-temurin:21-jre-jammy) is Python 3.10.
    ("convert", "tifffile"): (
        "2024.12.12",
        "bioio-ome-tiff 1.4.0 caps tifffile<2025.1.10 on py<3.11",
    ),
    # The bioio 3.5.0 plugin set will not resolve against numpy 1.26.4 (bioio-lif's dask chain).
    # Safe HERE only: this image carries no TensorFlow and no StarDist, which are the sole reason
    # the harmonised numpy is held at the last 1.x.
    ("convert", "numpy"): (
        "2.2.6",
        "bioio 3.5.0 plugin set cannot resolve with numpy 1.26.4",
    ),
}

# Packages a container's FROM base image bakes in, so no `pip install` line for them ever
# appears in the Dockerfile -- a script importing one is not missing a dependency, the
# Dockerfile just never had to name it. Documented per-container, each naming the base image
# that provides it, the same way PINNED_EXCEPTIONS documents its upstream constraint.
BASE_IMAGE_PROVIDES = {
    # pytorch/pytorch:*-cuda*-cudnn*-runtime bakes in a CUDA-matched torch build; pip
    # installing a second, unpinned torch here would risk silently replacing that
    # CUDA-matched wheel with a mismatched one.
    "cellsam": {"torch"},
    "instanseg": {"torch"},
}

# Packages that are genuinely absent from a given image are fine; these are the ones that must
# never appear again, having been removed as unimported.
#
# aicsimageio went OFF this list when the CSE seg-quality path arrived, then back ON when
# containers/segeval was harmonised. Both moves were right at the time. It is back because the
# only thing it supplied -- a physical pixel size from OME metadata -- is always overridden by
# --pixel-size-um (modules/local/seg_quality_eval.nf always renders it), and because the OME-TIFF
# reader plugin caps tifffile below the harmonised 2025.5.10, making the pair unresolvable.
# bin/utils/cse/functions.py's get_voxel_volume/get_pixel_area still carry a lazy
# `from aicsimageio import AICSImage`, but nothing in bin/ calls either function -- they are dead
# in upstream 1.5.19 and the vendored copy is kept byte-identical -- so that import never runs.
FORBIDDEN = ("cellpose", "cucim", "cupy", "aicsimageio")

# Requirement tokens are parsed with ``packaging`` rather than a regex. A hand-rolled pattern
# missed two real forms already present in this repo -- extras (``dask[array]>=2024.8.0``) and
# multi-clause specifiers (``numpy>=1.26,<3``) -- and silently reported both as "not installed",
# which reads as a missing dependency rather than as a parser gap. ``packaging`` ships with
# pytest, so it is available wherever this suite runs.
_NAME_ONLY = re.compile(r"^[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_.,\-]+\])?$")


def _parse_req(token):
    """(name, [(op, version), ...]) or None if the token is not a requirement."""
    try:
        r = Requirement(token)
    except InvalidRequirement:
        return None
    return r.name.lower(), [(s.operator, s.version) for s in r.specifier]


def _is_exact(specs):
    return len(specs) == 1 and specs[0][0] == "=="


# Module-level, so an unreadable or empty constraints file fails at COLLECTION rather than
# leaving each parametrised check to pass over an empty table. Defined after _parse_req
# because it uses it.
HARMONISED = _read_constraints()


def _dockerfile(name):
    return (CONTAINERS / name / "Dockerfile").read_text()


def _container_dirs():
    return sorted(p.name for p in CONTAINERS.iterdir() if (p / "Dockerfile").is_file())


def _dockerfile_pip_tokens(text):
    """Package tokens from a Dockerfile's pip-install commands only.

    Scoped to ``pip install`` lines on purpose. An earlier version also treated any
    non-directive line as a requirement, which harvested prose from RUN bodies and echo
    strings ("can", "wrapper", "nextflow") as if they were installed packages -- noise that
    could mask a genuinely missing dependency by matching its name.

    Split on ``&&`` BEFORE searching for ``pip install``, and search every resulting segment,
    not just the first. This is load-bearing for containers/tiled, which chains three commands
    on one continuation-joined line (``pip install -r requirements.txt && pip install
    torch==... && pip install kornia==...`` -- the order is required, since kornia drags the
    CUDA torch wheel from PyPI if torch is not already satisfied). Taking only the text up to
    the first ``&&`` silently dropped torch and kornia, which would have made this guard pass
    while torch/kornia were missing from the counted set -- reporting a container "installs
    everything its scripts import" when the packages the DISK+LightGlue front-end actually
    needs were invisible to it. The defect was first found in the now-deleted containers/
    stare-ml image; the same chained form is what containers/tiled uses today.
    """
    joined = re.sub(r"\\\s*\n", " ", text)
    tokens = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for segment in stripped.split("&&"):
            segment = segment.strip()
            if not re.search(r"pip3?\s+install", segment):
                continue
            body = re.split(r"pip3?\s+install", segment, maxsplit=1)[1]
            for tok in body.split():
                if tok.startswith("git+"):
                    # `pip install git+https://github.com/<owner>/<repo>.git@<rev>` names no
                    # requirement token at all -- the bare "/" skip below would otherwise drop
                    # it entirely, silently treating a real (pinned-by-commit) install as if the
                    # package were never installed. Recover the package name from the repo path
                    # segment. cellsam's cellSAM is the live example (regqc's cudipy was
                    # removed 2026-08-31 -- its upstream repository no longer exists).
                    m = re.search(r"/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:@.*)?$", tok)
                    if m:
                        tokens.append(m.group(1))
                    continue
                if tok.startswith(("-", "$")) or "/" in tok:
                    continue
                tokens.append(tok.strip('"').strip("'"))
    return tokens


def _requirements_tokens(path):
    tokens = []
    for line in path.read_text().splitlines():
        stripped = line.split("#")[0].split(";")[0].strip()
        if stripped:
            tokens.append(stripped)
    return tokens


def _requirements_files_installed(text):
    """Every `requirements/<name>.txt` a Dockerfile actually `pip install -r`s.

    The files moved out of ``containers/<c>/requirements.txt`` into ``requirements/<c>.txt``
    so a single ``constraints.txt`` could be shared with CI. This resolves them by the
    BASENAME the Dockerfile installs rather than by the container's own name, because
    containers/tiled installs three (tiled.txt, torch-cpu.txt, kornia.txt) -- and reading only
    ``tiled.txt`` would have hidden torch and kornia from every check below, which is exactly
    the "counted set is smaller than the installed set" defect this module's parser docstring
    describes.
    """
    joined = re.sub(r"\\\s*\n", " ", text)
    found = []
    for m in re.finditer(r"(?:-r|--requirement)[=\s]+(\S+)", joined):
        path = REQUIREMENTS / Path(m.group(1)).name
        if path.is_file():
            found.append(path)
    return found


def _installed(container):
    """package -> specifier string including its operator, or None if installed bare."""
    text = _dockerfile(container)
    tokens = _dockerfile_pip_tokens(text)
    # Only count a requirements file the Dockerfile actually installs.
    for req in _requirements_files_installed(text):
        tokens += _requirements_tokens(req)
    found = {}
    for tok in tokens:
        parsed = _parse_req(tok)
        if not parsed:
            continue
        name, specs = parsed
        if name in ("pip", "setuptools", "wheel", "upgrade"):
            continue
        if name not in found or not found[name]:
            found[name] = specs
    return found


@pytest.mark.parametrize("container", _container_dirs())
def test_every_requirements_file_is_actually_installed(container):
    """A requirements file nothing installs is a decoy that reads like a pin."""
    req = REQUIREMENTS / f"{container}.txt"
    if not req.is_file():
        pytest.skip(f"{container} has no requirements/{container}.txt")
    text = _dockerfile(container)
    assert re.search(rf"COPY[^\n]*requirements/{container}\.txt", text), (
        f"requirements/{container}.txt is never COPYd into containers/{container}, so every "
        f"version it lists is inert. Either install it or delete it."
    )
    assert req in _requirements_files_installed(text), (
        f"requirements/{container}.txt is COPYd but never pip-installed."
    )


@pytest.mark.parametrize("container", _container_dirs())
def test_no_shared_package_is_installed_unpinned(container):
    """An unpinned shared package makes the image non-reproducible and can change major version."""
    installed = _installed(container)
    # The island may use ">=" (it tracks spatialdata's own floor); everyone else needs "==".
    ok = (lambda s: bool(s)) if container == PY312_ISLAND else _is_exact
    unpinned = sorted(p for p, v in installed.items() if p in HARMONISED and not ok(v))
    assert not unpinned, (
        f"containers/{container} installs {unpinned} without an exact version. A rebuild can "
        f"therefore produce a different image than the one that generated published results."
    )


@pytest.mark.parametrize("container", _container_dirs())
def test_shared_packages_match_the_harmonised_version(container):
    """One version per package across every image that is not the py3.12 island."""
    if container == PY312_ISLAND:
        pytest.skip(f"{container} is the documented python:3.12 / zarr>=3 exception")
    installed = _installed(container)
    wrong = {
        p: (v[0][1], HARMONISED[p])
        for p, v in installed.items()
        if p in HARMONISED
        and _is_exact(v)
        and v[0][1] != HARMONISED[p]
        and PINNED_EXCEPTIONS.get((container, p), (None, None))[0] != v[0][1]
    }
    assert not wrong, (
        f"containers/{container} disagrees with the harmonised set "
        f"(package: found -> expected): {wrong}"
    )


@pytest.mark.parametrize("container", _container_dirs())
def test_no_forbidden_package_reappears(container):
    """Frameworks removed for having zero importers must not drift back in."""
    text = _dockerfile(container)
    for req in _requirements_files_installed(text):
        text += "\n" + req.read_text()
    # Strip `#` comments first. This scan is a regex over raw text, so without this a comment
    # EXPLAINING why a package was removed -- quoting the import line it used to have -- reads as
    # a reinstall and fails the guard. That is backwards: it pressures the next person to delete
    # the rationale rather than keep it. Both Dockerfiles and requirements files comment with `#`.
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    back = sorted(
        {f for f in FORBIDDEN if re.search(rf"(?m)^\s*{f}\b|[\s\\]{f}[=\s\\]", text)}
    )
    assert not back, (
        f"containers/{container} reinstalls {back}, which was removed for having no importer "
        f"anywhere in bin/. If it is genuinely needed now, add the import first."
    )


def _seg_backends_container_scripts():
    """(container dir, {scripts}) for SEGMENT's three backends (modules/local/segment.nf).

    SEGMENT resolves BOTH its container (``SegBackends.container(params.seg_method)``) and
    its entrypoint script (``backend.entrypoint``) dynamically from ``lib/SegBackends.groovy``
    -- never a literal ``bolt3x/mirage-<name>:`` tag or a literal ``<script>.py \\`` line in
    segment.nf itself -- so the regex scan in ``_module_container_and_scripts`` cannot see
    stardist/instanseg/cellsam at all. Parsed straight out of the same table segment.nf reads
    at runtime, not hand-duplicated, so this stays in sync with the table by construction.
    """
    text = (REPO / "lib" / "SegBackends.groovy").read_text()
    out = {}
    for m in re.finditer(
        r"container\s*:\s*'bolt3x/mirage-([a-z0-9-]+):[^']*'.*?entrypoint\s*:\s*'([a-z0-9_]+\.py)'",
        text,
        re.DOTALL,
    ):
        out.setdefault(m.group(1), set()).add(m.group(2))
    return out


def _module_container_and_scripts():
    """(container dir, {scripts}) for every modules/local/*.nf naming a first-party image,
    plus SEGMENT's backend-dispatched images resolved through lib/SegBackends.groovy (see
    ``_seg_backends_container_scripts``).

    There is deliberately no third source for profile-bound container overrides. There used to
    be: ``-profile stare_ml`` re-pointed TILED_COARSE at a second image, and a container
    reachable only through a profile never appears in any ``modules/local/*.nf``, so it needed
    its own scan. That profile is gone -- torch/kornia moved into :tiled itself -- and
    ``nextflow.config`` now contains no ``withName: '...' { container = ... }`` override at
    all, so the scan returned ``{}`` and both it and its helper were dead code that nothing
    would have flagged. If a profile-bound override is ever reintroduced, restore that scan
    WITH a non-vacuity assertion; without one it silently covers nothing.

    WARP_SEG_QC (modules/local/warp_seg_qc.nf) resolves its container the same
    backend-dispatched way, via ``lib/WarpBackends.groovy``, but is deliberately NOT given
    the same treatment here. Its VALIS backend's image (``cdgatenbee/valis-wsi``) is not a
    first-party ``bolt3x/mirage-*`` image and has no ``containers/`` entry to check against;
    its tiled backend's image (``bolt3x/mirage-tiled``) already gets script coverage from
    tiled_coarse.nf / tiled_reg_tile.nf / etc above. Attributing ``warp_seg_qc.py`` itself to
    'tiled' would be unsound the way SEGMENT's attribution is not: SEGMENT has three separate
    per-backend entrypoint FILES (segment.py / segment_instantseg.py / segment_cellsam.py),
    each installed and run only under its own container, so its whole static import graph is
    a fair claim on that container. ``warp_seg_qc.py`` is ONE file dispatched by a `--method`
    flag read at runtime (``_main_tiled`` vs. the VALIS path in ``main``/``write_report``), so
    its static import graph includes VALIS-only deferred imports (``valis``, ``scyjava``) that
    never execute under `--method tiled` -- attributing them to the tiled container would flag
    a false missing dependency, not a real one.
    """
    out = {}
    for nf in sorted((REPO / "modules" / "local").glob("*.nf")):
        text = nf.read_text()
        ref = re.search(r"bolt3x/mirage-([a-z0-9-]+):", text)
        if not ref:
            continue
        cdir = ref.group(1)
        if not (CONTAINERS / cdir / "Dockerfile").is_file():
            continue
        scripts = set(re.findall(r"([a-z0-9_]+\.py)\s*\\", text))
        if scripts:
            out.setdefault(cdir, set()).update(scripts)
    for cdir, scripts in _seg_backends_container_scripts().items():
        if (CONTAINERS / cdir / "Dockerfile").is_file():
            out.setdefault(cdir, set()).update(scripts)
    return out


# Taken from the interpreter rather than hand-listed: a hand-list silently reports stdlib
# modules as missing dependencies (``__future__``, ``statistics``), which is a guard failing
# for the wrong reason -- the exact failure mode this repo has been bitten by before.
_STDLIB_OK = set(sys.stdlib_module_names)

# Import name -> pip distribution name, where they differ.
_IMPORT_TO_DIST = {
    "skimage": "scikit-image",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "bioio": "bioio",
    "instanseg": "instanseg-torch",
}


def _third_party_imports(script):
    """Top-level distributions a script imports, parsed with ``ast``.

    Deliberately not a regex: ``^\\s*(import|from)\\s+(\\w+)`` also matches prose inside
    docstrings ("import the module first"), which reported a package named ``the`` as a
    missing dependency. Only a real parse distinguishes an import statement from a sentence.
    """
    local_files = {p.stem: p for p in (REPO / "bin").rglob("*.py")}
    # Local PACKAGES, not just modules. `local_files` is keyed by file stem, so a
    # package directory is invisible to it -- `bin/utils/cse/__init__.py` has the stem
    # `__init__`, and `from cse import single_method_eval` therefore looked like a
    # third-party import that containers/segeval failed to install. It is vendored
    # source, staged onto $PATH by Nextflow like the rest of bin/, and pip installs
    # nothing for it. `utils` used to be hardcoded here for exactly this reason;
    # deriving the set covers it and every future vendored package alike.
    local_pkgs = {
        d.name
        for d in (REPO / "bin").rglob("*")
        if d.is_dir() and (d / "__init__.py").is_file()
    }
    third, seen, queue = set(), set(), [script]
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        path = local_files.get(Path(cur).stem)
        if path is None or not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                head = (
                    node.module.split(".")[0]
                    if (node.level == 0 and node.module)
                    else None
                )
                names = [head] if head else []
                # ``from utils.tiled_io import open_lazy`` -- follow into the submodule, but ONLY
                # when the head is itself local. Taking the last component unconditionally turned
                # ``from skimage.transform import warp`` into a package named "transform".
                if head in local_files or head in local_pkgs:
                    if node.module and "." in node.module:
                        names.append(node.module.split(".")[-1])
                if node.level:
                    # A RELATIVE import is local by definition -- there is no such thing as a
                    # relative third-party import. Follow the module it names, and treat the
                    # imported SYMBOLS as symbols: queue the ones that are themselves local
                    # modules, and discard the rest rather than reporting them as missing
                    # packages. Without this, the vendored `bin/utils/cse` package's own
                    # ``from .functions import cell_size_uniformity, ...`` was read as a dozen
                    # third-party dependencies that containers/segeval "failed to install".
                    if node.module:
                        names.append(node.module.split(".")[-1])
                    names += [a.name for a in node.names if a.name in local_files]
                elif head is None or head in local_pkgs:
                    names += [a.name for a in node.names]
            else:
                continue
            for n in names:
                if n in local_files or n in local_pkgs:
                    queue.append(n)
                elif n not in _STDLIB_OK:
                    third.add(n)
    return third


#: Shared reason for the (container, bioio/h5py) exemptions below: every writer-only script
#: reaches both names through the same shared module, for the same reason.
_OME_IO_READER_DISPATCH_UNREACHABLE = (
    "bin/utils/ome_io.py's bioio/h5py imports are lazy, INSIDE its reader-dispatch "
    "functions (_open_bioio and read_info's h5py branch), reached only through "
    "detect_reader/require_reader/read_info/read_plane. apply_basic_profiles.py, "
    "tiled_stitch.py, merge_channels_pyramid.py, extract_mask_series.py, segment.py, "
    "segment_cellsam.py, segment_instantseg.py, split_multichannel.py and tile_for_basic.py "
    "all import write_tiff from the same module -- the write seam, not the read one -- and "
    "never call a reader-dispatch name, so the import never runs in these containers. "
    "bioio/h5py stay containers/convert-only, per ome_io.py's own module docstring. Guarded "
    "by test_ome_io_reader_dispatch_is_unreachable_from_the_writer_only_scripts."
)

#: Reason for the (regqc, bioio) exemption specifically. NOT the same claim as the one above:
#: bin/utils/qc.py's create_registration_qc DOES call read_info/read_plane directly (plan
#: 08's before/after panel, reading the native pre-registration plane) -- so, unlike every
#: script covered by _OME_IO_READER_DISPATCH_UNREACHABLE, the dispatch names themselves are
#: genuinely reached in this container. What stays unreached is specifically _open_bioio and
#: the hdf5 branch INSIDE read_info/read_plane, because every path those two ever see at this
#: call site -- reference_path, registered_path and native_path -- is one of this pipeline's
#: own .ome.tif intermediates (CONVERT_IMAGE emits `*.ome.tif`, modules/local/convert_image.nf:13;
#: REGISTER stages `*.ome.tif`/`*.ome.tiff`, modules/local/register.nf:103), which
#: ome_io._TIFFFILE_READABLE covers -- so read_info/read_plane always take the tifffile
#: branch and _open_bioio is dead code for THIS caller specifically. Guarded by
#: test_qc_reader_dispatch_stays_tifffile_only, a narrower and separate claim from the one
#: test_ome_io_reader_dispatch_is_unreachable_from_the_writer_only_scripts checks for the
#: other seven containers above.
_QC_READER_DISPATCH_STAYS_TIFFFILE_ONLY = (
    "bin/utils/qc.py's create_registration_qc calls ome_io.read_info/read_plane directly "
    "(plan 08's before/after panel), so -- unlike the writer-only scripts above -- the "
    "dispatch names ARE called in this container. bioio stays unreached because every path "
    "reaching read_info/read_plane here (reference_path, registered_path, native_path) is "
    "this pipeline's own .ome.tif/.ome.tiff intermediate (CONVERT_IMAGE/REGISTER's own "
    "output convention), which ome_io._TIFFFILE_READABLE covers, so _open_bioio is never "
    "reached from this caller. Guarded by test_qc_reader_dispatch_stays_tifffile_only."
)

# Imports the static graph reaches but that can never EXECUTE. Narrow on purpose: keyed by
# (container, imported name), each with the reason, and each reason guarded by its own test below
# so an exemption cannot outlive the fact that justified it.
UNREACHABLE_IMPORTS = {
    ("segeval", "aicsimageio"): (
        "bin/utils/cse/functions.py's get_voxel_volume/get_pixel_area open with a lazy "
        "`from aicsimageio import AICSImage` and then never use the name -- it is dead in "
        "upstream CellSegmentationEvaluator 1.5.19, and bin/utils/cse/ is kept byte-identical to "
        "upstream on purpose. Nothing in bin/ calls either function, so the import never runs. "
        "Guarded by test_unreachable_import_exemptions_are_still_unreachable."
    ),
    ("preprocess", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("preprocess", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("tiled", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("tiled", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("merge", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("merge", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    # segment.py/segment_cellsam.py/segment_instantseg.py (Task 7) each added a direct
    # `from ome_io import write_tiff`, and every one of them already imports image_utils.py
    # (also now `from ome_io import write_tiff`) for `ensure_dir`. Neither route calls a
    # reader-dispatch name.
    ("stardist", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("stardist", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("cellsam", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("cellsam", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("instanseg", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("instanseg", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    # export_geojson.py, extract_cell_properties.py and quantify.py all import image_utils.py
    # for ensure_dir/load_image; image_utils.py's save_tiff now delegates to ome_io.write_tiff.
    ("quantify", "bioio"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    ("quantify", "h5py"): _OME_IO_READER_DISPATCH_UNREACHABLE,
    # generate_registration_qc.py imports bin/utils/qc.py, which imports ome_io.write_tiff
    # for its composite writes AND (plan 08) ome_io.read_info/read_plane for the native
    # pre-registration plane. containers/regqc already installs h5py (requirements/regqc.txt)
    # for its own vendor readers, so only bioio is missing -- see
    # _QC_READER_DISPATCH_STAYS_TIFFFILE_ONLY for why it stays unreached even though
    # read_info/read_plane themselves are now genuinely called.
    ("regqc", "bioio"): _QC_READER_DISPATCH_STAYS_TIFFFILE_ONLY,
    # There is deliberately NO ("tiled", "torch")/("tiled", "kornia") entry. Both used to be
    # exempted here on the grounds that torch/kornia shipped "only in the stare-ml container",
    # so :tiled was never expected to satisfy the import. containers/tiled now installs both,
    # which makes that reason false -- and a false exemption is worse than none, because
    # test_container_installs_what_its_scripts_import `continue`s past an exempted (container,
    # import) pair and would stop checking a dependency that genuinely has to be there.
}


def test_unreachable_import_exemptions_are_still_unreachable():
    """The premise of every UNREACHABLE_IMPORTS entry, checked rather than trusted.

    An exemption that outlives its reason is worse than no exemption: it silently permits the
    exact runtime ImportError the guard exists to catch. containers/segeval has already shipped
    one of those -- matplotlib is imported on the live path by the same vendored file.
    """
    dead = ("get_voxel_volume", "get_pixel_area")
    callers = []
    for path in (REPO / "bin").rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            for fn in dead:
                if f"{fn}(" in code and not code.lstrip().startswith("def "):
                    callers.append(f"{path.relative_to(REPO)}:{lineno}")
    assert not callers, (
        "UNREACHABLE_IMPORTS exempts containers/segeval from installing aicsimageio because "
        f"{' / '.join(dead)} are never called -- but they are, at {callers}. The lazy "
        "`from aicsimageio import AICSImage` inside them now executes, so segeval must install "
        "aicsimageio (or the call must go)."
    )


def test_ome_io_reader_dispatch_is_unreachable_from_the_writer_only_scripts():
    """The premise behind every (preprocess|tiled|merge|stardist|cellsam|instanseg|quantify,
    bioio|h5py) exemption above.

    Each script below imports something from bin/utils/ome_io.py -- directly (write_tiff)
    or transitively, through image_utils.py's own `from ome_io import write_tiff` -- which
    is the write seam, never the read one. That module's ONLY bioio/h5py imports are lazy,
    inside its reader-dispatch functions (detect_reader/require_reader/read_info/read_plane
    and the private helpers they call). If any of these scripts starts calling one of those
    names, this fails and the exemption must be reconsidered rather than assumed to still
    hold.

    NOT checked here: utils/qc.py and generate_registration_qc.py. Plan 08's before/after
    panel made create_registration_qc call read_info/read_plane directly for the native
    plane, so the "never calls a reader-dispatch name" premise this test checks is no longer
    true for those two -- see test_qc_reader_dispatch_stays_tifffile_only and
    _QC_READER_DISPATCH_STAYS_TIFFFILE_ONLY for the (regqc, bioio) exemption's own, narrower
    claim instead.
    """
    dispatch_names = ("detect_reader", "require_reader", "read_info", "read_plane")
    writer_only_scripts = (
        "apply_basic_profiles.py",
        "tiled_stitch.py",
        "merge_channels_pyramid.py",
        "extract_mask_series.py",
        "segment.py",
        "segment_cellsam.py",
        "segment_instantseg.py",
        "split_multichannel.py",
        "tile_for_basic.py",
        "utils/image_utils.py",
        # The three ENTRY scripts the quantify exemption comments above actually name as the
        # reason (they reach ome_io only transitively, through utils/image_utils.py) --
        # checked directly rather than assumed, so a dispatch call added straight to one of
        # these would be caught here too, not just in the module it currently reaches ome_io
        # through.
        "quantify.py",
        "export_geojson.py",
        "extract_cell_properties.py",
    )
    callers = []
    for rel in writer_only_scripts:
        path = REPO / "bin" / rel
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            for fn in dispatch_names:
                if f"{fn}(" in code:
                    callers.append(f"{rel}:{lineno} ({fn})")
    assert not callers, (
        "UNREACHABLE_IMPORTS exempts containers/preprocess, containers/tiled, "
        "containers/merge, containers/stardist, containers/cellsam, containers/instanseg "
        "and containers/quantify from installing bioio/h5py because none "
        f"of {writer_only_scripts} call ome_io's reader-dispatch functions -- but they now "
        f"do, at {callers}. The lazy bioio/h5py imports inside those functions now execute, "
        "so the affected container must install bioio/h5py (or the call must go)."
    )


def test_qc_reader_dispatch_stays_tifffile_only():
    """The premise behind the (regqc, bioio) exemption -- see
    _QC_READER_DISPATCH_STAYS_TIFFFILE_ONLY.

    Unlike the writer-only scripts test_ome_io_reader_dispatch_is_unreachable_from_the_writer_
    only_scripts checks, bin/utils/qc.py DOES call ome_io's reader-dispatch functions (plan
    08's before/after panel reads the native pre-registration plane through
    ome_io.read_info/read_plane). What keeps containers/regqc from needing bioio is narrower:
    those two functions only reach _open_bioio (the lazy `import bioio`) for a path outside
    ome_io._TIFFFILE_READABLE, and every path qc.py ever calls them with is one of this
    pipeline's own .ome.tif/.ome.tiff intermediates. This test pins both halves of that claim
    so a change to either -- qc.py OR generate_registration_qc.py calling a riskier dispatch
    name, or the pipeline's own producers drifting off the .ome.tif convention -- fails here
    rather than silently invalidating the exemption.

    The dispatch-name scan covers BOTH bin/utils/qc.py and bin/generate_registration_qc.py:
    the latter was dropped from test_ome_io_reader_dispatch_is_unreachable_from_the_writer_
    only_scripts's writer_only_scripts tuple alongside qc.py (it imports qc.py, so the same
    premise no longer holds for it either), but nothing else was scanning it for a dispatch
    call added directly to the CLI script itself.
    """
    called = {}
    for rel in ("utils/qc.py", "generate_registration_qc.py"):
        path = REPO / "bin" / rel
        found = set()
        for line in path.read_text().splitlines():
            # strip_line_comment, not `line.split("#", 1)[0]`: CLAUDE.md's "Verification
            # reality" #7 forbids a private comment-stripping regex, and the naive cut
            # truncates any line whose `#` is inside a string literal.
            code = strip_line_comment(line)
            for fn in ("detect_reader", "require_reader", "read_info", "read_plane"):
                if f"{fn}(" in code:
                    found.add(fn)
        called[rel] = found
    assert called["utils/qc.py"], (
        "qc.py no longer calls any ome_io reader-dispatch function -- the (regqc, bioio) "
        "exemption should go back to _OME_IO_READER_DISPATCH_UNREACHABLE (the same claim as "
        "the other seven containers), and this test (now vacuous) should be removed."
    )
    # detect_reader is pure extension-sniffing (no import); require_reader is what a NEW,
    # riskier read path would call to force a specific backend. Neither is what
    # create_registration_qc calls -- if one appears (in EITHER file), the "always tifffile"
    # branch reasoning below no longer applies and the exemption needs re-deriving, not just
    # re-reading.
    for rel, found in called.items():
        assert found <= {"read_info", "read_plane"}, (
            f"bin/{rel} now calls {found - {'read_info', 'read_plane'}}, not just "
            "read_info/read_plane -- the (regqc, bioio) exemption's reasoning (read_info/"
            "read_plane only reach _open_bioio for a non-_TIFFFILE_READABLE suffix) does not "
            "cover detect_reader/require_reader and must be re-derived."
        )

    ome_io_src = (REPO / "bin" / "utils" / "ome_io.py").read_text()
    tifffile_readable_match = re.search(
        r"_TIFFFILE_READABLE\s*=\s*\(([^)]*)\)", ome_io_src
    )
    assert tifffile_readable_match, (
        "ome_io._TIFFFILE_READABLE not found by this guard's regex"
    )
    tifffile_readable = tifffile_readable_match.group(1)
    for suffix in (".ome.tif", ".ome.tiff", ".tif", ".tiff"):
        assert f'"{suffix}"' in tifffile_readable, (
            f"ome_io._TIFFFILE_READABLE no longer names {suffix!r} -- "
            "CONVERT_IMAGE/REGISTER's own output convention would then route through "
            "_open_bioio, and the (regqc, bioio) exemption is no longer sound."
        )

    # The pipeline-level half of the claim: reference_path/registered_path/native_path are
    # always CONVERT_IMAGE's or REGISTER's own output, never an arbitrary user-supplied
    # format. Read through tests.nfmodel rather than a raw .read_text() needle: a raw needle
    # is satisfied by a COMMENT that merely mentions the pattern -- measured here, a
    # `// historical: emitted path("*.ome.tif")` comment kept a raw check green while the
    # real declaration changed to something else entirely. strip_comments(process.raw_body)
    # removes exactly that comment while keeping the real string literal verbatim (CLAUDE.md's
    # "Verification reality" #5: the blanked `.body`/`.outputs` view is for LOCATING
    # structure and identifiers; a literal inside a quoted string needs the string-preserving
    # view instead).
    nf_processes = processes()
    convert_image = nf_processes["CONVERT_IMAGE"]
    assert '"*.ome.tif"' in strip_comments(convert_image.raw_body), (
        "CONVERT_IMAGE (modules/local/convert_image.nf) no longer emits '*.ome.tif' in real "
        "(non-comment) code -- the (regqc, bioio) exemption assumed CONVERT_IMAGE's output "
        "is always tifffile-readable, and that assumption needs re-checking against "
        "whatever it emits now."
    )
    register = nf_processes["REGISTER"]
    register_clean = strip_comments(register.raw_body)
    assert '"*.ome.tif"' in register_clean and '"*.ome.tiff"' in register_clean, (
        "REGISTER (modules/local/register.nf) no longer stages '*.ome.tif'/'*.ome.tiff' in "
        "real (non-comment) code -- the (regqc, bioio) exemption assumed REGISTER's output "
        "is always tifffile-readable, and that assumption needs re-checking against "
        "whatever it stages now."
    )


def _torch_kornia_import_sites(path):
    """[(enclosing function name or None, lineno), ...] for every ``import torch``/``import
    kornia`` (or ``from torch``/``from kornia`` ...) anywhere in ``path``, tagged with the
    innermost enclosing function -- ``None`` means module scope.
    """
    tree = ast.parse(path.read_text())
    sites = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Import(self, node):
            for alias in node.names:
                if alias.name.split(".")[0] in ("torch", "kornia"):
                    sites.append((self.stack[-1] if self.stack else None, node.lineno))
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if node.module and node.module.split(".")[0] in ("torch", "kornia"):
                sites.append((self.stack[-1] if self.stack else None, node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def test_tiled_container_torch_kornia_imports_are_confined_to_disk_lightglue():
    """torch/kornia must be imported ONLY inside ``_frontend_disk_lightglue``, never at module
    scope.

    This began as the premise behind two now-deleted UNREACHABLE_IMPORTS entries (torch/kornia
    used to ship in a separate image, so inside :tiled the import was meant to fail). :tiled
    now installs both, and the rule survives that change on its own reasoning: ``import torch``
    at module scope in ``coarse_align.py`` would make the module -- and therefore
    ``bin/tiled_coarse.py`` -- UNIMPORTABLE anywhere torch is absent. That is not hypothetical.
    ``coarse_align.py`` is imported by the tiled oracle (``bin/utils/tiled_pipeline.py``) and by
    this test suite, and it must keep importing on a plain checkout with no ML stack, so that
    ``estimate_transform_from_matches``, ``normalize_intensity``,
    ``scale_transform_to_full_res`` and every test that does not touch DISK keep working. It is
    also what turns a torch-less environment into an actionable RuntimeError at CALL time
    instead of an ImportError at import time.

    The confinement is what lets ``_disk_models`` take the model classes as ARGUMENTS instead
    of importing them; see the comment above it in coarse_align.py.
    """
    offenders = []
    for rel in ("bin/utils/coarse_align.py", "bin/tiled_coarse.py"):
        for fn, lineno in _torch_kornia_import_sites(REPO / rel):
            if fn != "_frontend_disk_lightglue":
                offenders.append(f"{rel}:{lineno} (in {fn or 'module scope'})")
    assert not offenders, (
        "torch/kornia is imported outside _frontend_disk_lightglue in a script the tiled "
        f'container runs: {offenders}. The ("tiled", "torch")/("tiled", "kornia") '
        "UNREACHABLE_IMPORTS exemptions assume the import is confined there; a second import "
        "site needs its own justification, not a free ride on this one."
    )


@pytest.mark.parametrize(
    "container,scripts", sorted(_module_container_and_scripts().items())
)
def test_container_installs_what_its_scripts_import(container, scripts):
    """The rule that catches bioio: an image must install what its own scripts import.

    A missing dependency here is invisible at build time and fails at gigapixel scale, after the
    scheduler has already granted the task its memory.
    """
    installed = set(_installed(container)) | BASE_IMAGE_PROVIDES.get(container, set())
    missing = {}
    for script in sorted(scripts):
        for name in sorted(_third_party_imports(script)):
            if (container, name) in UNREACHABLE_IMPORTS:
                continue
            dist = _IMPORT_TO_DIST.get(name, name).lower()
            if dist not in installed and name.lower() not in installed:
                missing.setdefault(script, []).append(name)
    assert not missing, (
        f"containers/{container} does not install everything its scripts import: {missing}. "
        f"The script runs in this image, so the import fails at runtime."
    )


@pytest.mark.parametrize("key", sorted(PINNED_EXCEPTIONS))
def test_every_pinned_exception_is_still_doing_something(key):
    """An exception that matches the harmonised value is stale and should be deleted.

    Without this, an exception silently outlives the upstream cap that justified it and becomes
    a permanent hole in the harmonised set that nobody re-examines.
    """
    container, package = key
    version, reason = PINNED_EXCEPTIONS[key]
    assert package in HARMONISED, (
        f"{package} is not in the harmonised set, so exempting it is meaningless"
    )
    assert version != HARMONISED[package], (
        f"the {container}/{package} exception pins {version}, which is what HARMONISED already "
        f"requires -- the exception is stale and should be removed."
    )
    assert reason.strip(), (
        "every exception must name the upstream constraint that forces it"
    )
    actual = _installed(container).get(package)
    assert _is_exact(actual) and actual[0][1] == version, (
        f"containers/{container} pins {package}={actual}, but the exception documents {version}. "
        f"The exception and the Dockerfile must agree, or the exception is describing fiction."
    )
