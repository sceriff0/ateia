#!/usr/bin/env bash
# Import smoke test for bolt3x/mirage-segeval.
#
# ONE DEFINITION, RUN IN TWO PLACES: this image's own Dockerfile RUNs it as a build
# step, and .github/workflows/containers.yml runs it again with `docker run` against
# the built image on a pull request that touched this build context. A stack that
# resolves in pip and then explodes on first import therefore fails the BUILD, not a
# cluster task six hours in -- scikit-image 0.25 against numpy 1.26 is a pairing pip
# will happily accept and then break on.
#
# WHAT IT ASSERTS, AND WHY THAT SET. Exactly the third-party modules the pipeline's
# own bin/ scripts import when they run in THIS image -- bin/seg_quality_eval.py and the vendored
# CellSegmentationEvaluator subset under bin/utils/cse/. MOVED HERE VERBATIM from
# this image's Dockerfile, which carried it inline; the content is unchanged.
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

"$PY" -c "\
import numpy, scipy, skimage, sklearn, pandas, tifffile, xmltodict; \
from sklearn.cluster import KMeans; \
from skimage.segmentation import find_boundaries; \
print('segeval image OK:', \
      'numpy', numpy.__version__, \
      '| scipy', scipy.__version__, \
      '| skimage', skimage.__version__, \
      '| sklearn', sklearn.__version__, \
      '| tifffile', tifffile.__version__)"

"$PY" -c "\
import matplotlib; matplotlib.use('Agg'); \
import matplotlib.pyplot; \
print('matplotlib OK -- functions.py foreground_separation() can import pyplot')"

# procps supplies `ps`. Nextflow's task-metrics wrapper hard-exits BEFORE the script
# block when it is absent, so every task in this image would fail with exit status 1
# and empty stdout as soon as the run collected metrics.
ps -e -o pid= -o ppid= > /dev/null && echo "procps OK: nextflow task-metrics wrapper can run"
