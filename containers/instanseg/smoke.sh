#!/usr/bin/env bash
# Import smoke test for bolt3x/mirage-instanseg.
#
# ONE DEFINITION, RUN IN TWO PLACES: this image's own Dockerfile RUNs it as a build
# step, and .github/workflows/containers.yml runs it again with `docker run` against
# the built image on a pull request that touched this build context. A stack that
# resolves in pip and then explodes on first import therefore fails the BUILD, not a
# cluster task six hours in -- scikit-image 0.25 against numpy 1.26 is a pairing pip
# will happily accept and then break on.
#
# WHAT IT ASSERTS, AND WHY THAT SET. Exactly the third-party modules the pipeline's
# own bin/ scripts import when they run in THIS image -- bin/segment_instantseg.py, whose InstanSeg import
# is lazy (inside the function) and so is never exercised by the pip resolve alone.
# That rule is what makes the list neither a guess nor decoration: every name below
# is imported on a live run, so a name that stops importing is a broken image by
# definition. Do not add a module this image's processes never import (an assertion
# nothing depends on fails the build for no user-visible reason); do not drop one
# they do.
set -euo pipefail

# `python` is NOT present in every base used here -- containers/convert installs
# python3 with no `python` alternative -- so resolve the interpreter rather than
# assuming a name.
PY="$(command -v python || command -v python3)"

"$PY" -c "
import numpy, tifffile
from instanseg import InstanSeg
print('instanseg image OK:',
      'numpy', numpy.__version__,
      '| tifffile', tifffile.__version__,
      '| InstanSeg', InstanSeg.__name__)"
