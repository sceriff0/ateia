"""ASHLAR comparator drivers, harness-owned.

ASHLAR is the field-standard external cyclic-IF registrar and the benchmark's only
EXTERNAL baseline. It is deliberately NOT a Mirage registration backend -- dev removed
that at :fire: 6a54479 and guards it with tests/test_ashlar_backend_removed.py -- so a
ranking that includes it has to drive it itself. That is what this package is.

``solve`` rewrites ashlar's per-tile placements into the SAME M0 + mesh manifest STARE
emits, which is the whole reason ashlar is comparable at all: bin/warp_seg_qc.py
--method tiled -- the PIPELINE'S OWN reg_qc=2 seg-overlap scorer -- reads it unchanged,
so ashlar is scored on the same nuclei, by the same code, into the same
<root>/<arm>/<patient>/qc/registration/*_seg_qc.json tree as every VALIS and STARE arm.

benchmarks/run_ashlar_arm.sh is the driver that chains the three steps (retile -> solve
-> score); build_arm_plan.py plans it as an arm_kind='external' row.

Scored any other way it would produce numbers in a different metric family sharing no
column with the arm table -- exactly what the earlier out-of-band ashlar harness did, and
what the deleted synthetic ground-truth rung did after it.
"""
