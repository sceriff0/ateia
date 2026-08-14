/*
========================================================================================
    PatientGroup PROBE — the grouping that must REFUSE to run unsized
========================================================================================
    tests/lib_probe.nf holds every PatientGroup assertion that can pass. This script
    holds the ones that cannot: a grouping whose size is missing must ABORT THE RUN,
    so the only way to observe it is a script whose expected outcome is a nonzero
    exit. That cannot live in lib_probe.nf, which asserts `workflow.success`.

    Why this needs a script at all: `nextflow_function` in nf-test can only call a
    function defined in a `.nf` file, and PatientGroup is a lib/ class. The probe
    pattern (a pipeline script compiled WITH lib/ on the classpath, plus an nf-test
    that only asserts how it exited) is tests/lib_probe.nf's, and the `-lib` mechanics
    are documented in tests/lib_probe.nf.test's header.

    THE DEFECT THIS PINS. Before lib/PatientGroup.groovy, five of the six sized
    per-patient groupings in this pipeline were written as

        def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count) : meta.patient_id

    -- a ternary whose else-branch is an UNSIZED key. An unsized groupTuple cannot
    know a key is complete until the ENTIRE upstream channel closes, so one missing
    count turns every per-patient stage into a full-run barrier: patient B waits for
    patient A's last slide, the run still exits 0, and nothing anywhere says so.
    tiled_adapter.nf's requireTilesCount was the one site that failed loudly, and it
    is the precedent PatientGroup generalises.

    `params.pg_case` selects which shape aborts:
      patient  the linear per-patient shape (PatientGroup.byPatient, meta.patient_id)
      slide    the per-slide shape (PatientGroup.byKey, a compound key) — pins that
               the error names the COMPUTED GROUP KEY, not just the patient
========================================================================================
*/

workflow {

    if (params.pg_case == 'patient') {
        PatientGroup.byPatient(
            Channel.of([[patient_id: 'P001', id: 'P001_ref'], 'ref.tiff']),
            name  : 'REGISTRATION: the per-patient slide group feeding REGISTER',
            size  : 'images_count',
            sortBy: { meta, _f -> meta.id },
        ).subscribe { row -> println "GROUPED UNSIZED: ${row}" }
    }
    else if (params.pg_case == 'slide') {
        PatientGroup.byKey(
            Channel.of([[patient_id: 'P002', channels: ['DAPI']], 'tile.json']),
            name  : 'TILED_ADAPTER: the control-point gather feeding TILED_SOLVE',
            size  : 'tiles_count',
            key   : { meta -> "${meta.patient_id}#${meta.channels.toSorted().join('_')}".toString() },
            sortBy: { _meta, control -> control },
        ).subscribe { row -> println "GROUPED UNSIZED: ${row}" }
    }
    else {
        throw new IllegalArgumentException("unknown --pg_case '${params.pg_case}'")
    }
}
