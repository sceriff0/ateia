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
# THE `HOME=/nonexistent` READ IS THE POINT. bffile fetches ome:formats-gpl through
# scyjava/jgo into ~/.jgo + ~/.m2 at first use; the Dockerfile baked those under /root and
# made them world-readable. This proves the reader OPENS A FILE with no usable $HOME --
# the cluster's actual condition -- rather than merely that the package imports.
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

# Read a real file through Bio-Formats with NO usable $HOME. scyjava computes its jgo
# cache_dir and m2_repo from Path.home() at import time and never consults the
# JGO_CACHE_DIR/M2_REPO environment variables, so the timing-immune lever is
# scyjava.config.set_cache_dir/set_m2_repo -- the same two calls
# bin/utils/jvm_cache.point_jvm_cache_off_readonly_home() makes at run time.
HOME=/nonexistent "$PY" -c "
import scyjava.config as sjconf
sjconf.set_cache_dir('/root/.jgo')
sjconf.set_m2_repo('/root/.m2/repository')
import numpy, tifffile
tifffile.imwrite('/tmp/smoke-probe.tif', numpy.zeros((8, 8), 'uint16'))
from bioio_bioformats import Reader
print('Bio-Formats reads with no HOME:', Reader('/tmp/smoke-probe.tif').dims)"

# procps supplies `ps`. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when it is absent, and params.enable_trace defaults to true, so every task in
# this image would fail with exit status 1 and empty stdout.
ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"
