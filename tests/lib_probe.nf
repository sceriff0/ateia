/*
========================================================================================
    lib/ PROBE — the unit-test surface for lib/*.groovy
========================================================================================
    WHY THIS IS A .nf FILE AND NOT AN nf-test ASSERTION BLOCK.

    nf-test's assertion context is a separate Groovy shell that does NOT have the
    project's lib/ directory on its classpath: naming `Layout` there fails with
    `No such property: Layout` (see the header of tests/layout.nf.test). A
    Nextflow PIPELINE SCRIPT, by contrast, is compiled WITH lib/ on the
    classpath — so the assertions live here, in a script, and
    tests/lib_probe.nf.test only has to assert that this script ran.

    Run directly:   nextflow run tests/lib_probe.nf -lib lib
    Run in CI:      the nextflow-stub job, and tests/lib_probe.nf.test (tag: stub)

    This file declares no processes and touches no filesystem. Every statement is
    a pure call into a lib/ class plus an `assert`. A failing assert aborts the
    run with a nonzero exit — that is the whole mechanism.

    APPENDING: later tasks add their own assert blocks under a clearly commented
    section header, following the pattern below. Keep each class's assertions in
    its own banner-delimited section.
========================================================================================
*/

workflow {

    // ------------------------------------------------------------------ //
    // Layout — checkpoint paths
    // ------------------------------------------------------------------ //
    assert Layout.checkpointDir('/out')                              == '/out/csv'
    assert Layout.checkpointDir('/out/')                             == '/out/csv'
    assert Layout.checkpointCsvName(Layout.REGISTERED)               == 'registered.csv'
    assert Layout.checkpointCsvRelative(Layout.POSTPROCESSED)        == 'csv/postprocessed.csv'
    assert Layout.checkpointCsv('/out', Layout.PREPROCESSED)         == '/out/csv/preprocessed.csv'

    // A step name that is not a checkpoint step must throw, not silently produce
    // '<outdir>/csv/nonsense.csv'.
    def badStep = false
    try { Layout.checkpointCsvName('nonsense') }
    catch (IllegalArgumentException ignored) { badStep = true }
    assert badStep, 'Layout.checkpointCsvName must reject an unknown step'

    // A blank outdir must throw rather than produce a literal 'null/csv'.
    def badOutdir = false
    try { Layout.checkpointDir('  ') }
    catch (IllegalArgumentException ignored) { badOutdir = true }
    assert badOutdir, 'Layout.checkpointDir must reject a blank outdir'

    // ------------------------------------------------------------------ //
    // MarkerUtils — the nuclear-marker rule
    // ------------------------------------------------------------------ //
    // markerList normalises three incompatible shapes. The bare-String case is the
    // dangerous one: iterated as a List it yields CHARACTERS, so 'PANCK' would match
    // any channel containing 'C'.
    assert MarkerUtils.markerList(['DAPI', 'CELLTOX']) == ['DAPI', 'CELLTOX']
    assert MarkerUtils.markerList('DAPI,CELLTOX')      == ['DAPI', 'CELLTOX']
    assert MarkerUtils.markerList('DAPI CELLTOX')      == ['DAPI', 'CELLTOX']
    assert MarkerUtils.markerList('PANCK')             == ['PANCK']

    // Matching is case-insensitive SUBSTRING, deliberately: 'DAPI_nuclear' is nuclear.
    assert MarkerUtils.isNuclear('DAPI_nuclear', ['DAPI'])
    assert MarkerUtils.isNuclear('dapi', ['DAPI'])
    assert !MarkerUtils.isNuclear('CD3', ['DAPI'])

    // MARKER PREFERENCE ORDER BEATS CHANNEL ORDER. With markers ['DAPI','CELLTOX'] and
    // channels ['CELLTOX','CD8','DAPI'] the answer is 2, not 0. SEGMENT's CellSAM
    // backend feeds this straight to --dapi-channel, so a wrong answer segments the
    // wrong channel rather than failing.
    assert MarkerUtils.nuclearIndex(['CELLTOX', 'CD8', 'DAPI'], ['DAPI', 'CELLTOX']) == 2
    assert MarkerUtils.nuclearIndex(['CD8', 'CELLTOX'], ['DAPI', 'CELLTOX'])         == 1

    assert MarkerUtils.hasNuclear(['CD3', 'DAPI'], ['DAPI'])
    assert !MarkerUtils.hasNuclear(['CD3', 'CD8'], ['DAPI'])

    // splitOutputChannels: a reference keeps every channel; a non-reference drops the
    // nuclear one. This must stay identical to bin/split_multichannel.py's runtime rule.
    assert MarkerUtils.splitOutputChannels(['DAPI', 'CD3'], true,  ['DAPI']) == ['DAPI', 'CD3']
    assert MarkerUtils.splitOutputChannels(['DAPI', 'CD3'], false, ['DAPI']) == ['CD3']

    // ------------------------------------------------------------------ //
    // ParamUtils — the step vocabulary
    // ------------------------------------------------------------------ //
    assert ParamUtils.STEP_ORDER == ['preprocessing', 'registration', 'postprocessing']
    assert ParamUtils.entryColumnForStep('registration')      == 'preprocessed_image'
    assert ParamUtils.requiredColumnsForStep('preprocessing') ==
        ['patient_id', 'path_to_file', 'is_reference', 'channels']

    assert  ParamUtils.shouldRun('registration',   'preprocessing', 'postprocessing')
    assert !ParamUtils.shouldRun('preprocessing',  'registration',  'postprocessing')
    assert !ParamUtils.shouldRun('postprocessing', 'preprocessing', 'registration')

    def badTarget = false
    try { ParamUtils.shouldRun('segmentation', 'preprocessing', 'postprocessing') }
    catch (IllegalArgumentException ignored) { badTarget = true }
    assert badTarget, 'ParamUtils.shouldRun must reject an unknown step'

    // Every step's entryColumn must be one of its own requiredColumns, or the
    // samplesheet parser reads a column validation never demanded.
    ParamUtils.STEPS.each { step ->
        assert step.entryColumn in step.requiredColumns,
            "step '${step.name}': entryColumn '${step.entryColumn}' not in requiredColumns"
    }

    // ------------------------------------------------------------------ //
    // Checkpoint — filename AND columns, one owner
    // ------------------------------------------------------------------ //
    assert Checkpoint.columns(Layout.PREPROCESSED) ==
        ['patient_id', 'preprocessed_image', 'is_reference', 'channels']
    assert Checkpoint.columns(Layout.REGISTERED) ==
        ['patient_id', 'registered_image', 'is_reference', 'channels']
    assert Checkpoint.columns(Layout.POSTPROCESSED) ==
        ['patient_id', 'cell_csv', 'cell_geojson', 'merged_csv', 'cell_mask', 'pyramid']

    // The header IS the seed: string the three writers pass to collectFile. These
    // three literals are the published contract — Group A must not change them.
    assert Checkpoint.header(Layout.PREPROCESSED)  == 'patient_id,preprocessed_image,is_reference,channels'
    assert Checkpoint.header(Layout.REGISTERED)    == 'patient_id,registered_image,is_reference,channels'
    assert Checkpoint.header(Layout.POSTPROCESSED) == 'patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid'

    // row() emits values in DECLARED COLUMN ORDER regardless of map insertion order.
    // This is the whole point: a writer can no longer transpose two columns.
    assert Checkpoint.row(Layout.REGISTERED, [
        channels: 'DAPI|CD3', patient_id: 'P001',
        registered_image: '/out/P001/registered/x.ome.tiff', is_reference: false
    ]) == 'P001,/out/P001/registered/x.ome.tiff,false,DAPI|CD3'

    // A missing column must throw, not silently emit an empty field — an empty field
    // is a checkpoint row naming a path that does not exist, which is exactly the
    // failure csv/postprocessed.csv shipped with for two releases.
    def missingCol = false
    try { Checkpoint.row(Layout.REGISTERED, [patient_id: 'P001']) }
    catch (IllegalArgumentException ignored) { missingCol = true }
    assert missingCol, 'Checkpoint.row must reject a missing column'

    // An unknown key must throw too — it means the caller thinks the schema is
    // something it is not.
    def unknownCol = false
    try {
        Checkpoint.row(Layout.REGISTERED, [
            patient_id: 'P001', registered_image: '/x', is_reference: false,
            channels: 'DAPI', typo_column: 'oops'
        ])
    }
    catch (IllegalArgumentException ignored) { unknownCol = true }
    assert unknownCol, 'Checkpoint.row must reject an unknown column'

    // Checkpoint and Layout must agree on the step vocabulary. Two tables that drift
    // is the exact failure this extraction exists to prevent.
    assert Checkpoint.STEPS*.name == Layout.CHECKPOINT_STEPS

    // println, NOT log.info: nf-test's underlying `nextflow ... -quiet` run
    // suppresses log.info from stdout entirely (observed directly: a log.info
    // line here never appears in workflow.stdout under nf-test, even though the
    // exact same script printed it fine under a plain `nextflow run`). println
    // writes straight to stdout regardless of -quiet, so it is what
    // tests/lib_probe.nf.test's `workflow.stdout.any { ... }` assertion needs.
    println "LIB PROBE: all assertions passed"
}
