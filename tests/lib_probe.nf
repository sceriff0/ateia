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

    // requireNuclearIndex — the rule TILED_COARSE and TILED_REG_TILE now share instead of
    // carrying fifteen byte-identical lines each. Both processes MUST resolve the same
    // index for a slide: COARSE fits M0 on that channel and REG_TILE measures each tile's
    // residual on it, so a drift between the two copies registered against the wrong marker
    // without failing.
    assert MarkerUtils.requireNuclearIndex(null, ['CD3', 'CELLTOX'], ['DAPI', 'CELLTOX'], 'P', 'p1') == 1
    // an explicit override wins over what metadata would resolve to...
    assert MarkerUtils.requireNuclearIndex(0, ['CD3', 'CELLTOX'], ['DAPI', 'CELLTOX'], 'P', 'p1') == 0
    // ...and 0 is a real override, not "unset" — the null check must not be a truthiness check.
    assert MarkerUtils.requireNuclearIndex(0, ['CD3', 'CD8'], ['DAPI'], 'P', 'p1') == 0

    def noNuclear = false
    try { MarkerUtils.requireNuclearIndex(null, ['CD3', 'CD8'], ['DAPI'], 'TILED_COARSE', 'p9') }
    catch (IllegalArgumentException e) {
        noNuclear = true
        // the message must still name the process, the slide and the configured markers
        assert e.message.contains('TILED_COARSE')
        assert e.message.contains('p9')
        assert e.message.contains('DAPI')
    }
    assert noNuclear, 'requireNuclearIndex must throw when no channel is nuclear'

    // A negative EXPLICIT override is an operator error, not a request to fall back.
    def badOverride = false
    try { MarkerUtils.requireNuclearIndex(-1, ['DAPI', 'CD3'], ['DAPI'], 'P', 'p1') }
    catch (IllegalArgumentException ignored) { badOverride = true }
    assert badOverride, 'a negative --reg_tiled_nuclear_index must throw'

    // ------------------------------------------------------------------ //
    // TilePlan — the tile-plan CSV schema, one owner
    // ------------------------------------------------------------------ //
    assert TilePlan.header() == 'ix,iy,cx,cy,x0,y0,x1,y1,rx0,ry0,rx1,ry1'
    assert TilePlan.REG_TILE_COLUMNS.every { it in TilePlan.COLUMNS }
    // The stub row must carry EVERY column, in COLUMNS order — a short row is a stub
    // publishing a CSV shape the real TILED_COARSE never writes.
    assert TilePlan.row(TilePlan.STUB_TILE) == '0,0,8,8,0,0,16,16,0,0,16,16'
    assert TilePlan.row(TilePlan.STUB_TILE).split(',').size() == TilePlan.COLUMNS.size()

    // A column name IS a flag name: this is the exact string TILED_REG_TILE renders.
    assert TilePlan.regTileArgs(TilePlan.STUB_TILE) ==
        '--ix 0 --iy 0 --cx 8 --cy 8 --rx0 0 --ry0 0 --rx1 16 --ry1 16'

    // A row missing a column must throw rather than render `--rx0 null`, which
    // bin/tiled_reg_tile.py would reject only after the container had started.
    def shortRow = false
    try { TilePlan.regTileArgs([ix: 0, iy: 0, cx: 8, cy: 8]) }
    catch (IllegalArgumentException ignored) { shortRow = true }
    assert shortRow, 'regTileArgs must throw on a row missing a consumed column'

    def shortPlanRow = false
    try { TilePlan.row([ix: 0, iy: 0]) }
    catch (IllegalArgumentException ignored) { shortPlanRow = true }
    assert shortPlanRow, 'TilePlan.row must throw on a row missing a column'

    // ------------------------------------------------------------------ //
    // ParamUtils — the step vocabulary
    // ------------------------------------------------------------------ //
    // Fixture root, used by the ParamUtils and Checkpoint sections below.
    // `projectDir` for this script is <repo>/tests (it is the script's own directory,
    // not the repo root — confirmed for both `nextflow run tests/lib_probe.nf -lib lib`
    // and nf-test's invocation), so fixtures are one level down from here.
    def fixtures = "${projectDir}/testdata"

    assert ParamUtils.STEP_ORDER == ['preprocessing', 'registration', 'segmentation', 'postprocessing']
    assert ParamUtils.entryColumnForStep('registration')      == 'preprocessed_image'
    assert ParamUtils.requiredColumnsForStep('preprocessing') ==
        ['patient_id', 'path_to_file', 'is_reference', 'channels']

    assert  ParamUtils.shouldRun('registration',   'preprocessing', 'postprocessing')
    assert !ParamUtils.shouldRun('preprocessing',  'registration',  'postprocessing')
    assert !ParamUtils.shouldRun('postprocessing', 'preprocessing', 'registration')

    // 'segmentation' is now a REAL step (added by this task) and must not be used
    // here as the "unknown step" example any more -- use a name that is genuinely
    // absent from STEPS instead.
    def badTarget = false
    try { ParamUtils.shouldRun('quantification', 'preprocessing', 'postprocessing') }
    catch (IllegalArgumentException ignored) { badTarget = true }
    assert badTarget, 'ParamUtils.shouldRun must reject an unknown step'

    // Every step's entryColumn must be one of its own requiredColumns, or the
    // samplesheet parser reads a column validation never demanded.
    ParamUtils.STEPS.each { step ->
        // requiredColumnsForStep, not step.requiredColumns: for an entry point whose
        // input is a checkpoint the field is absent and the list comes from
        // Checkpoint. Reading the raw field here would compare against `null` and
        // pass vacuously for three of the four steps.
        assert step.entryColumn in ParamUtils.requiredColumnsForStep(step.name),
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

    // A step whose ENTRY IS A CHECKPOINT requires exactly that checkpoint's columns.
    // The two tables used to state it independently and had already drifted:
    // postprocessing listed six of segmented.csv's eight, so CsvUtils.validateInputCSV
    // accepted a file Checkpoint.read then rejected a few lines later -- two answers
    // to "what must this file contain", checked in sequence.
    //
    // The derivation runs reader-from-WRITER, and the direction is the point.
    // Checkpoint.columns IS the writer's header seed (Checkpoint.header), so deriving
    // it from an entry contract instead would let a lax reader shrink a published
    // file. requiredColumnsForStep asks Checkpoint; Checkpoint never asks ParamUtils.
    assert ParamUtils.requiredColumnsForStep('registration')   == Checkpoint.columns(Layout.PREPROCESSED)
    assert ParamUtils.requiredColumnsForStep('segmentation')   == Checkpoint.columns(Layout.REGISTERED)
    assert ParamUtils.requiredColumnsForStep('postprocessing') == Checkpoint.columns(Layout.SEGMENTED)

    // preprocessing is the one entry point whose input is a real samplesheet rather
    // than a checkpoint this pipeline wrote, so it keeps a literal list and no
    // entryCheckpoint. That asymmetry is the whole reason the field is nullable.
    assert ParamUtils.STEPS.find { it.name == 'preprocessing' }.entryCheckpoint == null
    assert ParamUtils.STEPS.findAll { it.entryCheckpoint }*.entryCheckpoint ==
        [Layout.PREPROCESSED, Layout.REGISTERED, Layout.SEGMENTED]
    // Every entryCheckpoint must be a checkpoint step that really exists, or
    // requiredColumnsForStep would throw at the first --start that used it.
    ParamUtils.STEPS.findAll { it.entryCheckpoint }.each {
        assert it.entryCheckpoint in Layout.CHECKPOINT_STEPS
    }

    // The tightening, stated as behaviour rather than as a list: a segmented.csv
    // missing the two contour columns is now refused by the ENTRY validator, where it
    // used to be waved through and refused later by Checkpoint.read with a worse
    // message.
    def partialSheet = "${fixtures}/invalid_checkpoint_segmented_partial.csv"
    def partialRejected = false
    try { CsvUtils.validateInputCSV(partialSheet, ParamUtils.requiredColumnsForStep('postprocessing')) }
    catch (IllegalArgumentException e) {
        partialRejected = true
        assert e.message.contains('contours')
    }
    assert partialRejected,
        'validateInputCSV must reject a segmented.csv missing columns the writer emits'
    // ...and the same file is still refused by the reader, so the two layers agree on
    // the verdict and differ only in which one speaks first.
    def partialReadRejected = false
    try { Checkpoint.read(Layout.SEGMENTED, partialSheet, [nuclear_markers: 'DAPI']) }
    catch (IllegalArgumentException ignored) { partialReadRejected = true }
    assert partialReadRejected, 'Checkpoint.read must still reject the same partial checkpoint'

    // columns() must hand out an IMMUTABLE list. Checkpoint.STEPS' outer list is
    // .asImmutable() but a naive implementation leaves each entry's inner `columns`
    // ArrayList mutable, so a caller mutating the returned list permanently corrupts
    // the schema every later header()/row() call in the run sees. Demonstrated before
    // the fix: `Checkpoint.columns(Layout.PREPROCESSED) << 'injected'` succeeded
    // silently and mutated the live table in place, so a subsequent header() call
    // returned 'patient_id,preprocessed_image,is_reference,channels,injected'.
    def columnsThrew = false
    try { Checkpoint.columns(Layout.PREPROCESSED) << 'injected' }
    catch (UnsupportedOperationException ignored) { columnsThrew = true }
    assert columnsThrew, 'Checkpoint.columns() must return an immutable list; mutating it must throw'
    assert Checkpoint.columns(Layout.PREPROCESSED) ==
        ['patient_id', 'preprocessed_image', 'is_reference', 'channels'],
        'Checkpoint.columns() schema must be unaffected by the mutation attempt above'

    // ------------------------------------------------------------------ //
    // The artifact vocabulary
    // ------------------------------------------------------------------ //
    def kinds = ParamUtils.STEPS.collectMany { it.qcKinds } + ParamUtils.UNIVERSAL_QC_KINDS
    // Order is load-bearing here: the kind order is the report-slot order, so this is
    // an ordered-literal equality, not a set/sorted comparison. That same equality
    // already subsumes uniqueness -- a duplicate member makes this list one element
    // longer than the literal on the right, which fails equality before any separate
    // uniqueness check could run. (A prior version of this file carried a
    // `kinds.size() == kinds.toSet().size()` line after this assert; it was dead code,
    // unreachable by construction, and has been removed rather than kept "just in case".)
    assert kinds == ['preprocess_qc', 'registration_qc', 'registration_tre', 'seg_qc',
                     'seg_residuals', 'postprocess_qc', 'versions', 'size_log']

    // ------------------------------------------------------------------ //
    // The segmentation checkpoint
    // ------------------------------------------------------------------ //
    assert Layout.SEGMENTED == 'segmented'
    assert Layout.CHECKPOINT_STEPS == ['preprocessed', 'registered', 'segmented', 'postprocessed']
    assert Checkpoint.columns(Layout.SEGMENTED) == [
        'patient_id', 'registered_image', 'is_reference', 'channels',
        'cell_mask', 'nuclei_mask', 'contours', 'nucleus_contours',
    ]
    assert Checkpoint.header(Layout.SEGMENTED) ==
        'patient_id,registered_image,is_reference,channels,cell_mask,nuclei_mask,contours,nucleus_contours'

    // Empty string means "artifact not produced" — nucleus_contours is empty when
    // --quantify_compartments is false (nuclei_mask is NOT similarly gated: SEGMENT
    // always produces it — see Checkpoint's EMPTY VALUES note). row() must accept ''
    // (it is a value, not a missing key) and emit it as an empty field regardless of
    // which column carries it.
    assert Checkpoint.row(Layout.SEGMENTED, [
        patient_id: 'P001', registered_image: '/o/P001/registered/a.tif',
        is_reference: true, channels: 'DAPI|CD3',
        cell_mask: '/o/P001/segmentation/P001_cell_mask.tif',
        nuclei_mask: '/o/P001/segmentation/P001_nuclei_mask.tif',
        contours: '/o/P001/cell_properties/contours.json',
        nucleus_contours: '',
    ]) == 'P001,/o/P001/registered/a.tif,true,DAPI|CD3,/o/P001/segmentation/P001_cell_mask.tif,/o/P001/segmentation/P001_nuclei_mask.tif,/o/P001/cell_properties/contours.json,'

    // The step vocabulary gains one entry.
    assert ParamUtils.entryColumnForStep('segmentation') == 'registered_image'

    // ------------------------------------------------------------------ //
    // Layout.isUnderTaskDir / publishedOrAsIs — the fix for Critical 1
    // (--start segmentation recording paths that do not exist). A one-level
    // isTaskDir(path.parent) check cannot tell a FRESH one-level-nested output
    // (REGISTER's registered_slides/) from an ALREADY-PUBLISHED path with the
    // same producer-subdirectory name -- both have a parent literally named
    // 'registered_slides', which never matches the hex-hash pattern either way.
    // These six cases are exactly what review's own probe checked directly
    // against lib/, unreachable from any stub-run assertion at the workflow
    // level: reverting isUnderTaskDir to a parent-only check `isTaskDir(dir)`
    // makes case 2 fail (a live registered_slides/ output would be treated as
    // already-published and returned verbatim as its work-directory path).
    // ------------------------------------------------------------------ //
    def taskHash   = 'c' * 30
    def workDirPre = "/work/ab/${taskHash}"

    // 1. Fresh, flat (no producer subdirectory) -- e.g. SEGMENT's cell_mask.
    def freshFlat = file("${workDirPre}/P001_cell_mask.tif")
    assert Layout.isUnderTaskDir(freshFlat.parent)
    assert Layout.publishedOrAsIs('/out', 'P001', 'segmentation', freshFlat) ==
        Layout.publishedPath('/out', 'P001', 'segmentation', freshFlat)
    assert Layout.publishedOrAsIs('/out', 'P001', 'segmentation', freshFlat) ==
        '/out/P001/segmentation/P001_cell_mask.tif'

    // 2. Fresh, ONE level down -- REGISTER's registered_slides/. THE case a
    // parent-only isTaskDir check gets wrong (see the comment above).
    def freshNested = file("${workDirPre}/registered_slides/P001_x_registered.ome.tiff")
    assert Layout.isUnderTaskDir(freshNested.parent)
    assert Layout.publishedOrAsIs('/out', 'P001', Layout.REGISTERED, freshNested) ==
        '/out/P001/registered/registered_slides/P001_x_registered.ome.tiff'

    // 3. Already-published, same producer-subdirectory NAME as case 2 -- a prior
    // run's registered.csv entry read back via --start segmentation's INPUT_CHECK.
    // Neither the parent ('registered_slides') nor the grandparent ('registered')
    // is a task hash, so this must be returned AS-IS, not reconstructed against
    // the new outdir (that was Critical 1).
    def publishedNested = file('/prior/P001/registered/registered_slides/P001_x_registered.ome.tiff')
    assert !Layout.isUnderTaskDir(publishedNested.parent)
    assert Layout.publishedOrAsIs('/out', 'P001', Layout.REGISTERED, publishedNested) ==
        '/prior/P001/registered/registered_slides/P001_x_registered.ome.tiff'

    // 4. A single-slide passthrough, published under 'preprocessed/' but asked
    // about with kind REGISTERED -- the exact P002 shape from Critical 1's repro.
    // The kind argument must be IGNORED once the as-is branch fires: returning
    // the file's own path unchanged is what makes the wrong kind harmless here.
    def publishedPassthrough = file('/prior/P002/preprocessed/P002_ref_corrected.ome.tif')
    assert !Layout.isUnderTaskDir(publishedPassthrough.parent)
    assert Layout.publishedOrAsIs('/out', 'P002', Layout.REGISTERED, publishedPassthrough) ==
        '/prior/P002/preprocessed/P002_ref_corrected.ome.tif'

    // 5. Already-published, flat (no producer subdirectory) -- a prior run's
    // segmentation.nf cell_mask read back via --start postprocessing.
    def publishedFlat = file('/prior/P001/segmentation/P001_cell_mask.tif')
    assert !Layout.isUnderTaskDir(publishedFlat.parent)
    assert Layout.publishedOrAsIs('/out', 'P001', 'segmentation', publishedFlat) ==
        '/prior/P001/segmentation/P001_cell_mask.tif'

    // 6. Fresh, ONE level down via nuclei/ -- EXTRACT_NUCLEI_PROPERTIES's own
    // producer subdirectory (the fix for I5's published-path regression).
    def freshNuclei = file("${workDirPre}/nuclei/contours.json")
    assert Layout.isUnderTaskDir(freshNuclei.parent)
    assert Layout.publishedOrAsIs('/out', 'P001', 'cell_properties', freshNuclei) ==
        '/out/P001/cell_properties/nuclei/contours.json'

    // passthroughPath now delegates to publishedOrAsIs pinned to PREPROCESSED --
    // assert the delegation is behaviourally identical, not just present.
    assert Layout.passthroughPath('/out', 'P001', freshFlat) ==
        Layout.publishedOrAsIs('/out', 'P001', Layout.PREPROCESSED, freshFlat)
    assert Layout.passthroughPath('/out', 'P002', publishedPassthrough) ==
        Layout.publishedOrAsIs('/out', 'P002', Layout.PREPROCESSED, publishedPassthrough)

    // ------------------------------------------------------------------ //
    // ParamUtils.compartmentMode -- the
    // --quantify_compartments seam (mirrors --registration_method's: resolved
    // once, threaded down as an argument; see workflows/mirage.nf's
    // `compartment_mode` and tests/test_compartment_mode_routing.py).
    // ------------------------------------------------------------------ //

    // 1. Plain field mapping, all three flags on.
    def modeAllOn = ParamUtils.compartmentMode([
        quantify_compartments: true, quantify_statistics: ['Median','Mean','Sum'], embed_masks: true,
    ])
    assert modeAllOn == [compartments: true, statistics: ['Median','Mean','Sum'], embedMasks: true]

    // 2. All three off.
    def modeAllOff = ParamUtils.compartmentMode([
        quantify_compartments: false, quantify_statistics: ['Median'], embed_masks: false,
    ])
    assert modeAllOff == [compartments: false, statistics: ['Median'], embedMasks: false]

    // 3. The map is immutable -- a caller cannot mutate the shared snapshot out
    // from under another reader of the same resolved value.
    try {
        modeAllOn.compartments = false
        assert false : "compartmentMode() map must be immutable"
    } catch (UnsupportedOperationException ignored) {
        // expected
    }


    // ------------------------------------------------------------------ //
    // Checkpoint.read — the READ side of the schema owner
    // ------------------------------------------------------------------ //
    // The id rule INPUT_CHECK has always applied, now a named function so the
    // samplesheet reader and the checkpoint reader cannot drift. simpleName must
    // reproduce Nextflow's Path.simpleName exactly: everything from the FIRST dot
    // is an extension, and a leading dot belongs to the name.
    assert CsvUtils.simpleName('/a/b/P001_ref.ome.tiff') == 'P001_ref'
    assert CsvUtils.simpleName('/a/b/c.tif')             == 'c'
    assert CsvUtils.simpleName('/a/b/no_ext')            == 'no_ext'
    assert CsvUtils.simpleName('/a/b/.hidden.tif')       == '.hidden'
    // Prefix only when the stem does not already carry the patient id — the exact
    // rule input_check.nf applied inline.
    assert CsvUtils.imageId('P001', '/a/b/P001_ref.ome.tiff') == 'P001_ref'
    assert CsvUtils.imageId('P001', '/a/b/slideA.ome.tiff')   == 'P001_slideA'

    // A 'segmented' checkpoint: two rows, one patient.
    def segRows = Checkpoint.read(Layout.SEGMENTED,
                                  "${fixtures}/valid_checkpoint_segmented.csv",
                                  [nuclear_markers: 'DAPI'])
    assert segRows.size() == 2
    def segMetas = segRows.collect { it[0] }

    // THE meta shape: exactly the keys INPUT_CHECK emits, no more and no fewer.
    // An asserted key SET (not a spot-check of one key) is what makes this a shape
    // test — a reader that quietly stopped deriving channels_count would still pass
    // any per-key assertion that did not name it.
    def expectedKeys = ['channels', 'channels_count', 'id', 'images_count',
                        'is_reference', 'patient_id']
    segMetas.each { assert it.keySet().toList().toSorted() == expectedKeys }

    // `id` is the patient-prefixed source-image stem, NOT row.patient_id. This is
    // the behaviour change: at --start postprocessing every row used to collapse to
    // the same id, so two slides of one patient produced identically named outputs
    // at the one entry point where multiple patients share a collect.
    assert segMetas*.id == ['P001_ref', 'P001_mov1']
    assert segMetas*.id.unique().size() == 2
    assert segMetas*.patient_id == ['P001', 'P001']
    assert segMetas*.is_reference == [true, false]
    assert segMetas[0].channels == ['DAPI', 'PANCK', 'SMA']

    // Counts are DERIVED here, the same way INPUT_CHECK derives them: two images,
    // and five markers reaching quantification (the reference keeps its nuclear
    // channel — DAPI, PANCK, SMA — and the moving slide drops it, leaving CD3, CD8).
    assert segMetas.every { it.images_count == 2 }
    assert segMetas.every { it.channels_count == 5 }

    // The same shape from a different checkpoint step, given a different CSV: the
    // point of one reader is that the shape is the reader's, not the caller's.
    def regRows = Checkpoint.read(Layout.REGISTERED,
                                  "${fixtures}/prior_run/csv/registered.csv",
                                  [nuclear_markers: 'DAPI'])
    assert regRows.size() == 1
    assert regRows[0][0].keySet().toList().toSorted() == expectedKeys
    assert regRows[0][0].id == 'P001_image'
    assert regRows[0][0].is_reference
    assert regRows[0][0].images_count == 1
    assert regRows[0][0].channels_count == 2      // DAPI|PANCK, on the reference row

    // The row map is handed back untouched, keyed by column name — the same shape
    // splitCsv(header: true) gave the callers this reader replaces, so a consumer
    // still writes file(row.registered_image).
    assert regRows[0][1].registered_image.endsWith('P001_image.tiff')

    // 'postprocessed' declares neither is_reference nor channels, so its meta
    // carries neither — and neither a channels_count it could only have invented.
    // The shape is the SCHEMA's, and one row per patient is why `id` is the
    // patient_id here rather than an image stem.
    def postRows = Checkpoint.read(Layout.POSTPROCESSED,
                                   "${fixtures}/prior_run/csv/postprocessed.csv")
    assert postRows.size() == 1
    assert postRows[0][0].keySet().toList().toSorted() == ['id', 'images_count', 'patient_id']
    assert postRows[0][0].id == 'P001'

    // 'yes' must RAISE. It used to raise at one entry point and silently become
    // `false` at the other two, so a checkpoint whose reference row read 'yes'
    // became a checkpoint with no reference at all — and both readers carried on.
    def badRef = false
    try {
        Checkpoint.read(Layout.SEGMENTED,
                        "${fixtures}/invalid_checkpoint_segmented_bad_ref.csv",
                        [nuclear_markers: 'DAPI'])
    }
    catch (IllegalArgumentException e) {
        badRef = true
        assert e.message.contains('is_reference')
        assert e.message.contains('yes')
    }
    assert badRef, "Checkpoint.read must reject is_reference 'yes', not coerce it to false"

    // A checkpoint missing the column the counts are derived FROM must be refused by
    // name. A silent absence is the defect: a meta with no channels_count makes every
    // per-patient grouping take its unsized fallback, so streaming turns off for the
    // whole run and nothing reports it.
    def noChannels = false
    try {
        Checkpoint.read(Layout.SEGMENTED,
                        "${fixtures}/invalid_checkpoint_segmented_no_channels.csv",
                        [nuclear_markers: 'DAPI'])
    }
    catch (IllegalArgumentException e) {
        noChannels = true
        assert e.message.contains('channels')
    }
    assert noChannels, 'Checkpoint.read must reject a checkpoint missing a declared column'

    // requireCount, watched failing. THE guard requirement 4 is about, and until this
    // fixture existed nothing observed it raising: the "no channels column" case above
    // trips the HEADER check several lines earlier, so the count branch was a
    // defensive arm nobody had ever seen fire. Here every declared column is present
    // and every row parses -- the single row is simply non-reference and carries only
    // the nuclear channel, so MarkerUtils.splitOutputChannels drops it and the patient
    // reaches quantification with ZERO markers. channels_count is 0, and a groupKey
    // sized 0 never fills: the run would hang rather than fail.
    def zeroCount = false
    try {
        Checkpoint.read(Layout.SEGMENTED,
                        "${fixtures}/invalid_checkpoint_segmented_zero_markers.csv",
                        [nuclear_markers: 'DAPI'])
    }
    catch (IllegalStateException e) {
        zeroCount = true
        assert e.message.contains('channels_count')
        assert e.message.contains('P001')        // names the patient, not just the file
        assert e.message.contains('got 0')
    }
    assert zeroCount, 'Checkpoint.read must raise when a count derives to 0, not emit it'

    // The opts map is closed, and auto_reference is deliberately NOT a key. Auto-
    // promotion applies at --start preprocessing only (ParamUtils.autoReferenceAllowed
    // owns that rule), and preprocessing's input is a samplesheet, never a checkpoint --
    // so a checkpoint reader that accepted the flag would be re-encoding a rule it can
    // never legitimately exercise. Rejecting the key is what stops that copy coming
    // back silently, the same way Checkpoint.row rejects an unknown column.
    def unknownOpt = false
    try {
        Checkpoint.read(Layout.SEGMENTED, "${fixtures}/valid_checkpoint_segmented.csv",
                        [nuclear_markers: 'DAPI', auto_reference: true])
    }
    catch (IllegalArgumentException e) {
        unknownOpt = true
        assert e.message.contains('auto_reference')
    }
    assert unknownOpt, 'Checkpoint.read must reject an unknown option key'

    // A step that declares `channels` cannot derive channels_count without knowing
    // which markers are nuclear, and guessing 'DAPI' would be a second, silent
    // default for params.nuclear_markers.
    def noMarkers = false
    try { Checkpoint.read(Layout.SEGMENTED, "${fixtures}/valid_checkpoint_segmented.csv") }
    catch (IllegalArgumentException e) { noMarkers = true; assert e.message.contains('nuclear_markers') }
    assert noMarkers, 'Checkpoint.read must demand nuclear_markers when the step declares channels'

    // Unknown step: Checkpoint's own exception type, as everywhere else in this class.
    def badReadStep = false
    try { Checkpoint.read('nonsense', "${fixtures}/valid_checkpoint_segmented.csv") }
    catch (Checkpoint.UnknownStepException ignored) { badReadStep = true }
    assert badReadStep, 'Checkpoint.read must reject an unknown step'

    // A URI, not just a local path. Every file access in CsvUtils used to be
    // `new File(csvPath)`, which treats 's3://bucket/k.csv' as a RELATIVE LOCAL path
    // (a file 'k.csv' under a directory literally named 's3:'), so it silently does
    // not exist. That was harmless while these CSVs were only ever reached through
    // Channel.fromPath -- which resolves URIs through Nextflow's filesystem providers
    // -- and became a real narrowing when Checkpoint.read took over add_cycle's
    // --prior_outdir reads. CsvUtils.pathOf goes through the same provider lookup
    // Channel.fromPath uses.
    //
    // Proved WITHOUT a network: 'file://' is a genuine URI scheme with a provider, and
    // `new File('file:///abs/path')` does not exist while asPath resolves it.
    def fileUri = "file://${fixtures}/valid_checkpoint_segmented.csv"
    assert !(new File(fileUri).exists()), 'fixture is meant to be unreachable as a raw File path'
    def uriRows = Checkpoint.read(Layout.SEGMENTED, fileUri, [nuclear_markers: 'DAPI'])
    assert uriRows.size() == 2
    assert uriRows.collect { it[0].id } == ['P001_ref', 'P001_mov1']

    // A remote scheme resolves to that scheme's provider rather than to a local file.
    // Not read here (no credentials, no network) -- the resolution is the contract.
    assert CsvUtils.pathOf('s3://bucket/run/csv/registered.csv').toUriString() ==
        's3://bucket/run/csv/registered.csv'
    assert CsvUtils.pathOf('/tmp/plain.csv').toString() == '/tmp/plain.csv'

    // A missing file must name itself rather than surface as an empty channel.
    def missingFile = false
    try { Checkpoint.read(Layout.SEGMENTED, "${fixtures}/does_not_exist.csv", [nuclear_markers: 'DAPI']) }
    catch (FileNotFoundException ignored) { missingFile = true }
    assert missingFile, 'Checkpoint.read must reject a missing checkpoint CSV'

    // imageColumn is the third table this file cross-checks rather than trusts:
    // ParamUtils.STEPS names the column each `--start` reads, Checkpoint.STEPS names
    // the column each checkpoint's id is derived from, and for the three steps whose
    // entry IS a checkpoint those must be the same string.
    assert Checkpoint.imageColumn(Layout.PREPROCESSED) == ParamUtils.entryColumnForStep('registration')
    assert Checkpoint.imageColumn(Layout.REGISTERED)   == ParamUtils.entryColumnForStep('segmentation')
    assert Checkpoint.imageColumn(Layout.SEGMENTED)    == ParamUtils.entryColumnForStep('postprocessing')
    // postprocessed is the one checkpoint with no per-image column: one row per
    // patient, so there is no stem to prefix and `id` is the patient_id.
    assert Checkpoint.imageColumn(Layout.POSTPROCESSED) == null

    println "LIB PROBE: all assertions passed"
}
