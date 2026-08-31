"""ASHLAR comparator drivers, harness-owned.

ASHLAR is the field-standard external cyclic-IF registrar and the benchmark's only
EXTERNAL baseline. It is deliberately NOT a Mirage registration backend -- dev removed
that at :fire: 6a54479 and guards it with tests/test_ashlar_backend_removed.py -- so a
ranking that includes it has to drive it itself. That is what this package is.

``solve`` rewrites ashlar's per-tile placements into the SAME M0 + mesh manifest STARE
emits, which is the whole reason ashlar is comparable at all: the method-agnostic scorer
(benchmarks/stare_bench/cli.py, method="ashlar") reads it unchanged. Scored any other
way it would produce numbers in a different metric family sharing no column with the
table -- exactly what the earlier out-of-band ashlar harness did, and why it was deleted.
"""
