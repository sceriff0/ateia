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

REPO = Path(__file__).resolve().parent.parent
CONTAINERS = REPO / "containers"

# Since the one-repo-per-image rename, the component name in the image reference IS the
# build-context directory name, so no translation table is needed. The previous table mapped
# ten hand-written tags onto differently-spelled directories, which is precisely how
# `instant_seg` and `istantseg` came to misspell InstanSeg two different ways.

# The one image allowed to diverge, and why. It is python:3.12-slim and spatialdata>=0.8.0
# requires zarr>=3, which cannot be installed on the python:3.10 bases the others use.
PY312_ISLAND = "spatialdata"

# Packages installed by more than one image must agree on a version. Ceilings are the newest
# release that still supports Python 3.10, except numpy, which is held at the last 1.x because
# the segmentation image's TensorFlow 2.15 base requires numpy<2.
HARMONISED = {
    "numpy": "1.26.4",
    "tifffile": "2025.5.10",
    "imagecodecs": "2025.3.30",
    "zarr": "2.18.3",
    "numcodecs": "0.13.1",
    "pandas": "2.3.3",
    "scikit-image": "0.25.2",
    "scipy": "1.15.3",
    # Added after the final review found csbdeep and dask installed UNPINNED by the fix wave,
    # directly under a comment reading "Install Python packages with exact versions". They were
    # invisible to test_no_shared_package_is_installed_unpinned purely because that test only
    # checks packages listed here -- an unpinned package the table does not know about cannot be
    # flagged. Listing them is what makes the guard able to see them at all.
    "csbdeep": "0.8.2",
    "dask": "2026.7.1",
}

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
    ("convert", "tifffile"): ("2024.12.12", "bioio-ome-tiff 1.4.0 caps tifffile<2025.1.10 on py<3.11"),
    # The bioio 3.5.0 plugin set will not resolve against numpy 1.26.4 (bioio-lif's dask chain).
    # Safe HERE only: this image carries no TensorFlow and no StarDist, which are the sole reason
    # the harmonised numpy is held at the last 1.x.
    ("convert", "numpy"): ("2.2.6", "bioio 3.5.0 plugin set cannot resolve with numpy 1.26.4"),
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
    """
    joined = re.sub(r"\\\s*\n", " ", text)
    tokens = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not re.search(r"pip3?\s+install", stripped):
            continue
        body = re.split(r"pip3?\s+install", stripped, maxsplit=1)[1]
        body = body.split("&&")[0]
        for tok in body.split():
            if tok.startswith("git+"):
                # `pip install git+https://github.com/<owner>/<repo>.git@<rev>` names no
                # requirement token at all -- the bare "/" skip below would otherwise drop
                # it entirely, silently treating a real (pinned-by-commit) install as if the
                # package were never installed. Recover the package name from the repo path
                # segment: two containers do this (cellsam's cellSAM, regqc's cudipy).
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


def _installed(container):
    """package -> specifier string including its operator, or None if installed bare."""
    text = _dockerfile(container)
    req = CONTAINERS / container / "requirements.txt"
    tokens = _dockerfile_pip_tokens(text)
    # Only count a requirements file the Dockerfile actually installs.
    if req.is_file() and re.search(r"pip3?\s+install[^\n]*-r ", text):
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
    """A requirements.txt nothing installs is a decoy that reads like a pin."""
    req = CONTAINERS / container / "requirements.txt"
    if not req.is_file():
        pytest.skip(f"{container} has no requirements.txt")
    text = _dockerfile(container)
    assert re.search(r"COPY[^\n]*requirements", text), (
        f"containers/{container}/requirements.txt is never COPYd into the image, so every "
        f"version it lists is inert. Either install it or delete it."
    )
    assert re.search(r"pip3?\s+install[^\n]*-r ", text), (
        f"containers/{container}/requirements.txt is COPYd but never pip-installed."
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
    req = CONTAINERS / container / "requirements.txt"
    if req.is_file():
        text += "\n" + req.read_text()
    # Strip `#` comments first. This scan is a regex over raw text, so without this a comment
    # EXPLAINING why a package was removed -- quoting the import line it used to have -- reads as
    # a reinstall and fails the guard. That is backwards: it pressures the next person to delete
    # the rationale rather than keep it. Both Dockerfiles and requirements files comment with `#`.
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    back = sorted({f for f in FORBIDDEN if re.search(rf"(?m)^\s*{f}\b|[\s\\]{f}[=\s\\]", text)})
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
    local_pkgs = {d.name for d in (REPO / "bin").rglob("*")
                  if d.is_dir() and (d / "__init__.py").is_file()}
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
                head = node.module.split(".")[0] if (node.level == 0 and node.module) else None
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


@pytest.mark.parametrize("container,scripts", sorted(_module_container_and_scripts().items()))
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
    assert package in HARMONISED, f"{package} is not in the harmonised set, so exempting it is meaningless"
    assert version != HARMONISED[package], (
        f"the {container}/{package} exception pins {version}, which is what HARMONISED already "
        f"requires -- the exception is stale and should be removed."
    )
    assert reason.strip(), "every exception must name the upstream constraint that forces it"
    actual = _installed(container).get(package)
    assert _is_exact(actual) and actual[0][1] == version, (
        f"containers/{container} pins {package}={actual}, but the exception documents {version}. "
        f"The exception and the Dockerfile must agree, or the exception is describing fiction."
    )
