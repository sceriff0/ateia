#!/usr/bin/env bash
# Import smoke test for bolt3x/mirage-convert.
#
# ONE DEFINITION, RUN IN TWO PLACES: this image's own Dockerfile RUNs it as a build
# step, and .github/workflows/containers.yml runs it again with `docker run` against
# the built image on a pull request that touched this build context. A stack that
# resolves in pip and then explodes on first import therefore fails the BUILD, not a
# cluster task six hours in -- scikit-image 0.25 against numpy 1.26 is a pairing pip
# will happily accept and then break on.
#
# WHAT IT ASSERTS, AND WHY THAT SET. Exactly the third-party modules the pipeline's
# own bin/ scripts import when they run in THIS image -- bin/convert_image.py, plus the two Glencoe JVM
# tools it shells out to BY NAME (a missing symlink is otherwise a 'command not
# found' inside a Nextflow task, hours after the image was published).
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
from bioio import BioImage
print('convert image OK:',
      'numpy', numpy.__version__,
      '| tifffile', tifffile.__version__,
      '| bioio reader', BioImage.__name__)"

command -v bioformats2raw >/dev/null || { echo "bioformats2raw is not on PATH"; exit 1; }
command -v raw2ometiff   >/dev/null || { echo "raw2ometiff is not on PATH"; exit 1; }
echo "convert image OK: bioformats2raw and raw2ometiff are on PATH"
