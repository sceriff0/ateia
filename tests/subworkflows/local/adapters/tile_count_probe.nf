/*
 * Test-only probe workflow -- NOT part of the pipeline.
 *
 * Exists solely so tiled_adapter_group_size.nf.test can assert the invariant that actually
 * matters for the tiled fan-in's groupKey sizing (subworkflows/local/adapters/tiled_adapter.nf):
 * countTileRows(csv) must agree with the number of rows Nextflow's own
 * splitCsv(header: true) operator produces off the SAME file. A disagreement there is exactly
 * what would make the fan-in's groupKey wrong -- too high hangs the pipeline (the group never
 * closes), too low truncates the control points TILED_SOLVE fits its mesh from (the group closes
 * early on a silently mis-registered slide).
 *
 * countTileRows() cannot be exercised against a live splitCsv operator from inside an nf-test
 * `nextflow_function` test: Nextflow's Channel factory/operators are not available in a
 * function test's `then` block (only in the live script that sets up its `input[]`, which
 * itself cannot block on a dataflow result). A `nextflow_workflow` test against this tiny probe
 * is the straightforward way to run both derivations against the same real channel and compare.
 */
include { countTileRows } from '../../../../subworkflows/local/adapters/tiled_adapter.nf'

workflow TILE_COUNT_PROBE {
    take:
    ch_csv   // Channel of path -- a TILED_COARSE-shaped tile-plan CSV

    main:
    ch_derived     = ch_csv.map { csv -> countTileRows(csv) }
    ch_split_count = ch_csv.splitCsv(header: true).count()

    emit:
    derived     = ch_derived
    split_count = ch_split_count
}
