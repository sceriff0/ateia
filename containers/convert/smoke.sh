#!/usr/bin/env bash
# Import smoke test for bolt3x/mirage-convert.
#
# ONE DEFINITION, RUN IN TWO PLACES: this image's own Dockerfile RUNs it as a build
# step, and .github/workflows/containers.yml runs it again with `docker run` against
# the built image on a pull request that touched this build context. A stack that
# resolves in pip and then explodes on first import therefore fails the BUILD, not a
# cluster task six hours in.
#
# WHAT IT ASSERTS, AND WHY THAT SET. Exactly the third-party modules bin/convert_image.py
# imports at run time (numpy, tifffile, bioio, h5py), PLUS the Bio-Formats reader, which
# is the one whose failure mode is invisible to a pip resolve: bioio discovers its reader
# plugins through an entry point, so a broken bioio-bioformats is not an ImportError in
# convert_image.py -- it is a format the pipeline silently cannot read.
#
# THE POSITIVE LEG CALLS THE REAL PRODUCTION REDIRECT, NOT A HAND-ROLLED COPY OF IT.
# Review round 1 on Task 7 found that this script used to call
# `scyjava.config.set_cache_dir`/`set_m2_repo` directly -- two calls PRODUCTION NEVER
# MADE, because nothing on the CONVERT_IMAGE run path called
# `bin/utils/jvm_cache.py::point_jvm_cache_off_readonly_home()` at the time. That made
# this script pass while proving nothing about the real run path: the redirect it
# exercised and the redirect the pipeline uses were two different mechanisms that
# happened to agree. `bin/utils/ome_io.py::_open_bioio` and
# `bin/convert_image.py::read_image` now both call the guard for the bioio-bioformats
# route, and the Dockerfile COPYs the real `bin/utils/jvm_cache.py` into the image (on
# its own PYTHONPATH entry, never shadowing the copy Nextflow stages from the git
# checkout at run time) so this script can call the SAME function.
#
# THE `HOME=/nonexistent` READ IS THE POINT. bffile fetches ome:formats-gpl through
# scyjava/jgo into ~/.jgo + ~/.m2 at first use; the Dockerfile baked those under /root and
# made them world-readable. This proves the reader OPENS A FILE with no usable $HOME --
# the cluster's actual condition -- rather than merely that the package imports.
#
# THE NEGATIVE LEG PROVES THE LEVER IS LOAD-BEARING, NOT DECORATIVE. Run as root,
# `HOME=/nonexistent` alone proves nothing: root can `os.makedirs('/nonexistent/...')`
# freely (CAP_DAC_OVERRIDE), so a positive-only smoke test cannot tell "the redirect
# worked" apart from "root can write anywhere anyway". This leg drops privileges to
# UID 65534 (nobody) with `os.setuid`/`os.setgid` -- unprivileged, like
# nextflow.config's `docker.runOptions = '-u $(id -u):$(id -g)'` -- and skips the guard
# entirely, so scyjava is left with its Path.home()-derived default of
# `/nonexistent/.jgo`. Creating that directory needs write access to `/` (root-owned,
# mode 755), which UID 65534 does not have, so the read MUST fail. If it ever succeeds,
# either the redirect stopped being load-bearing or this leg stopped exercising the
# failure it claims to -- either way the smoke test should fail loudly, not stay quiet.
#
# The two Glencoe `command -v` assertions that used to be here are gone with the tools:
# nothing in the pipeline invoked bioformats2raw or raw2ometiff.
set -euo pipefail

# `python` is NOT present in every base used here -- containers/convert installs
# python3 with no `python` alternative -- so resolve the interpreter rather than
# assuming a name.
PY="$(command -v python || command -v python3)"

"$PY" -c "
import numpy, tifffile, h5py
from bioio import BioImage
import bioio_bioformats
print('convert image OK:',
      'numpy', numpy.__version__,
      '| tifffile', tifffile.__version__,
      '| h5py', h5py.__version__,
      '| bioio reader', BioImage.__name__,
      '| bioio-bioformats', bioio_bioformats.__name__)"

# The baked Maven artifacts. jgo creates the directory before it populates it, so the
# emptiness check is the one that matters.
test -d /root/.m2/repository/ome/formats-gpl
test -n "$(ls -A /root/.m2/repository/ome/formats-gpl)"
test -d /root/.jgo
echo "convert image OK: Bio-Formats jars are baked under /root/.m2 and /root/.jgo"

# POSITIVE LEG: read a real file through Bio-Formats with NO usable $HOME, via the
# PRODUCTION redirect (bin/utils/jvm_cache.py, COPYed in by the Dockerfile onto
# /usr/local/lib/mirage_jvm) -- the exact function bin/utils/ome_io.py::_open_bioio and
# bin/convert_image.py::read_image call for the bioio-bioformats route.
PYTHONPATH=/usr/local/lib/mirage_jvm HOME=/nonexistent "$PY" -c "
from jvm_cache import point_jvm_cache_off_readonly_home
point_jvm_cache_off_readonly_home()
import numpy, tifffile
tifffile.imwrite('/tmp/smoke-probe.tif', numpy.zeros((8, 8), 'uint16'))
from bioio_bioformats import Reader
print('Bio-Formats reads with no HOME (production redirect):',
      Reader('/tmp/smoke-probe.tif').dims)"

# NEGATIVE LEG: the same read, same HOME=/nonexistent, but WITHOUT the guard and
# dropped to an unprivileged UID first -- must FAIL. set +e/-e brackets the one command
# allowed to return non-zero.
set +e
HOME=/nonexistent "$PY" -c "
import os
os.setgid(65534)
os.setuid(65534)
import numpy, tifffile
tifffile.imwrite('/tmp/smoke-noguard-probe.tif', numpy.zeros((8, 8), 'uint16'))
from bioio_bioformats import Reader
Reader('/tmp/smoke-noguard-probe.tif')
"
noguard_status=$?
set -e
if [ "$noguard_status" -eq 0 ]; then
    echo "convert image FAIL: Bio-Formats read SUCCEEDED under HOME=/nonexistent with no jvm-cache redirect and no root DAC override -- the redirect is not load-bearing, or this leg is not exercising the failure it claims to" >&2
    exit 1
fi
echo "convert image OK: Bio-Formats read correctly FAILS (exit $noguard_status) with no jvm-cache redirect -- the positive leg's redirect above is load-bearing, not decorative"

# procps supplies `ps`. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when it is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout.
ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"
