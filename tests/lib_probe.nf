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

    // registeredPath -- the ONE rule for every csv/registered.csv row. It is
    // publishedPath pinned to REGISTERED, and it REFUSES a file emitted outside
    // Layout.REGISTERED_SUBDIR rather than recording a path nothing publishes. Both
    // halves asserted: the happy path, and the refusal (which is the half that used to
    // be a silent wrong answer -- a passthrough recorded under <pid>/registered/, or
    // under <pid>/preprocessed/ depending on which backend ran).
    def freshRegistered = file("${workDirPre}/${Layout.REGISTERED_SUBDIR}/P001_x_registered.ome.tiff")
    assert Layout.registeredPath('/out', 'P001', freshRegistered) ==
        "/out/P001/registered/${Layout.REGISTERED_SUBDIR}/P001_x_registered.ome.tiff"
    def refused = false
    try {
        Layout.registeredPath('/out', 'P001', freshFlat)
    } catch (IllegalArgumentException e) {
        refused = e.message.contains(Layout.REGISTERED_SUBDIR)
    }
    assert refused : 'Layout.registeredPath accepted a file emitted outside registered_slides/'

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

    // ------------------------------------------------------------------ //
    // PatientGroup — the one per-patient grouping
    // ------------------------------------------------------------------ //
    // pairAndSort IS the -resume cascade fix, in isolation. The worked example is
    // the one groupTuple(sort:) gets wrong: two items arriving as
    // (zeta, a.txt) and (alpha, z.txt). Sorting the two grouped lists
    // INDEPENDENTLY yields [alpha, zeta] and [a.txt, z.txt], so alpha is now
    // paired with a.txt -- silently, with no error anywhere. Pairing first and
    // sorting the PAIRS keeps each meta with its own file.
    def pgPairs = PatientGroup.pairAndSort(
        [[id: 'zeta'], [id: 'alpha']],
        ['a.txt', 'z.txt'],
        { meta, _f -> meta.id })
    assert pgPairs == [[[id: 'alpha'], 'z.txt'], [[id: 'zeta'], 'a.txt']]
    // Stated the other way round, because this is the assertion that would have
    // caught the bug: the grouped order changed, the PAIRING did not.
    assert pgPairs.collect { it[0].id } == ['alpha', 'zeta']
    assert pgPairs.collect { it[1] }    == ['z.txt', 'a.txt']

    // A sort key that does not separate two items leaves their relative order to
    // arrival -- i.e. it is not a canonical order at all, which is the whole point
    // of sorting here. That must be an error, not a silently partial ordering.
    def pgTied = false
    try { PatientGroup.pairAndSort([[id: 'a'], [id: 'a']], ['x', 'y'], { meta, _f -> meta.id }) }
    catch (IllegalStateException e) {
        pgTied = true
        assert e.message.contains('a')
    }
    assert pgTied, 'PatientGroup.pairAndSort must reject a sort key that ties'

    // transpose() on unequal lists TRUNCATES silently, which would drop a file.
    def pgRagged = false
    try { PatientGroup.pairAndSort([[id: 'a']], ['x', 'y'], { meta, _f -> meta.id }) }
    catch (IllegalStateException ignored) { pgRagged = true }
    assert pgRagged, 'PatientGroup.pairAndSort must reject unequal metas/payloads'

    // requireSize is what makes the unsized fallback unwritable. The message must
    // name the channel (which grouping refused), the meta key (what to inject) and
    // the group (whose meta was short) -- the run aborts inside an operator
    // closure, so the message is the only context the reader gets.
    assert PatientGroup.requireSize([images_count: 3], 'images_count', 'ch', 'P001') == 3
    ['a channel', 'images_count', 'P001'].each { needle ->
        def missing = false
        try { PatientGroup.requireSize([patient_id: 'P001'], 'images_count', 'a channel', 'P001') }
        catch (IllegalStateException e) {
            missing = true
            assert e.message.contains(needle), "requireSize message must name ${needle}"
        }
        assert missing, 'PatientGroup.requireSize must reject an absent size'
    }
    // 0 and a non-Integer are the same defect as absent: a groupKey sized 0 never
    // fills, so the run HANGS rather than fails. Checkpoint.requireCount refuses
    // both upstream for the same reason; this is the second, independent refusal.
    [0, -1, null, '3', 3.5].each { bad ->
        def rejected = false
        try { PatientGroup.requireSize([images_count: bad], 'images_count', 'ch', 'P001') }
        catch (IllegalStateException ignored) { rejected = true }
        assert rejected, "PatientGroup.requireSize must reject images_count=${bad}"
    }

    // End to end through real channel operators: the size hint is applied, the
    // GroupKey wrapper is unwrapped to a plain String (see
    // tests/test_group_key_unwrapped.py for why that is not cosmetic), and every
    // meta is still paired with its own payload in sort-key order. Asserted inside
    // .subscribe {} -- a failing assert there aborts the run with a nonzero exit,
    // which is the mechanism this whole probe is built on.
    PatientGroup.byPatient(
        Channel.of(
            [[patient_id: 'P001', id: 'P001_c', images_count: 3], 'c.tiff'],
            [[patient_id: 'P001', id: 'P001_a', images_count: 3], 'a.tiff'],
            [[patient_id: 'P001', id: 'P001_b', images_count: 3], 'b.tiff'],
        ),
        name  : 'lib probe: the per-patient grouping',
        size  : 'images_count',
        sortBy: { meta, _f -> meta.id },
    ).subscribe { row ->
        assert row[0] == 'P001'
        assert row[0].getClass() == String, 'the GroupKey wrapper must not escape the grouping'
        assert row[1].collect { it[0].id } == ['P001_a', 'P001_b', 'P001_c']
        assert row[1].collect { it[1] }    == ['a.tiff', 'b.tiff', 'c.tiff']
    }

    // byPatient IS byKey with the patient key bound. Handing it a `key` would be a
    // second, competing answer to "what is a patient group keyed on", so it is
    // refused rather than silently overridden either way.
    def pgKeyOpt = false
    try {
        PatientGroup.byPatient(Channel.empty(),
                               name: 'ch', size: 'images_count',
                               key: { meta -> meta.id }, sortBy: { m, f -> m.id })
    }
    catch (IllegalArgumentException ignored) { pgKeyOpt = true }
    assert pgKeyOpt, 'PatientGroup.byPatient must reject a caller-supplied key'

    // The opts map is closed, like Checkpoint.read's: a typo'd option must not be
    // accepted and then ignored, which is how `sortby:` would silently reinstate
    // arrival order.
    def pgUnknownOpt = false
    try {
        PatientGroup.byPatient(Channel.empty(),
                               name: 'ch', size: 'images_count',
                               sortBy: { m, f -> m.id }, sortby: { m, f -> m.id })
    }
    catch (IllegalArgumentException e) {
        pgUnknownOpt = true
        assert e.message.contains('sortby')
    }
    assert pgUnknownOpt, 'PatientGroup must reject an unknown option'

    // Every required option is required by NAME. A grouping missing `sortBy` is an
    // arrival-ordered grouping, which is the defect, not a default.
    ['name', 'size', 'sortBy'].each { opt ->
        def opts = [name: 'ch', size: 'images_count', sortBy: { m, f -> m.id }]
        opts.remove(opt)
        def omitted = false
        try { PatientGroup.byPatient(opts, Channel.empty()) }
        catch (IllegalArgumentException e) {
            omitted = true
            assert e.message.contains(opt)
        }
        assert omitted, "PatientGroup must require the '${opt}' option"
    }

    // ---- the DERIVED size: `sizeOf:` -------------------------------------
    // Three fan-ins size themselves from something no meta holds:
    // postprocess.nf's two per-patient QC gathers want ONE ARTIFACT PER MOVING
    // SLIDE, i.e. `meta.images_count - 1`. `size:` is a meta KEY read as-is and
    // cannot say that, so those sites kept hand-writing the very ternary this
    // class exists to delete:
    //
    //     def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count - 1)
    //                                 : meta.patient_id
    //
    // `sizeOf:` is the same guarantee for a derived size: mandatory, refused when
    // it does not resolve, never falling through to an unsized key.
    assert PatientGroup.requireDerivedSize(2, 'ch', 'P001') == 2
    ['a channel', 'P001'].each { needle ->
        def derivedMissing = false
        try { PatientGroup.requireDerivedSize(null, 'a channel', 'P001') }
        catch (IllegalStateException e) {
            derivedMissing = true
            assert e.message.contains(needle), "requireDerivedSize message must name ${needle}"
        }
        assert derivedMissing, 'PatientGroup.requireDerivedSize must reject an unresolved size'
    }
    // Same refusals as requireSize, for the same reasons: a groupKey sized 0 never
    // fills and the run HANGS, and a non-Integer is a closure that computed
    // something other than a count.
    [0, -1, null, '3', 3.5].each { bad ->
        def rejected = false
        try { PatientGroup.requireDerivedSize(bad, 'ch', 'P001') }
        catch (IllegalStateException ignored) { rejected = true }
        assert rejected, "PatientGroup.requireDerivedSize must reject ${bad}"
    }

    // End to end, with the size DERIVED rather than read: images_count counts the
    // reference too, so the QC gather's size is one less. Same guarantees as the
    // `size:` form — GroupKey unwrapped to a plain String, every meta still paired
    // with its own payload, in sort-key order.
    PatientGroup.byPatient(
        Channel.of(
            [[patient_id: 'P001', id: 'P001_c', images_count: 4], 'c.json'],
            [[patient_id: 'P001', id: 'P001_a', images_count: 4], 'a.json'],
            [[patient_id: 'P001', id: 'P001_b', images_count: 4], 'b.json'],
        ),
        name  : 'lib probe: the per-patient grouping, derived size',
        sizeOf: { meta, _f -> meta.images_count - 1 },
        sortBy: { _meta, f -> f },
    ).subscribe { row ->
        assert row[0] == 'P001'
        assert row[0].getClass() == String, 'the GroupKey wrapper must not escape the grouping'
        assert row[1].collect { it[0].id } == ['P001_a', 'P001_b', 'P001_c']
        assert row[1].collect { it[1] }    == ['a.json', 'b.json', 'c.json']
    }

    // EXACTLY ONE of size/sizeOf. Both is two competing answers to how big the
    // group is, and whichever the implementation happened to read first would win
    // silently — the same class of defect as an option accepted and ignored.
    def pgBothSizes = false
    try {
        PatientGroup.byPatient(Channel.empty(),
                               name: 'ch', size: 'images_count',
                               sizeOf: { m, _f -> m.images_count - 1 },
                               sortBy: { m, _f -> m.id })
    }
    catch (IllegalArgumentException e) {
        pgBothSizes = true
        assert e.message.contains('size')
        assert e.message.contains('sizeOf')
    }
    assert pgBothSizes, 'PatientGroup must reject size AND sizeOf together'

    // Neither is the unsized gather itself — a full-run barrier on a run that
    // still exits 0. It must be as unwritable as it was before sizeOf existed.
    def pgNoSize = false
    try {
        PatientGroup.byPatient(Channel.empty(), name: 'ch', sortBy: { m, _f -> m.id })
    }
    catch (IllegalArgumentException e) {
        pgNoSize = true
        assert e.message.contains('size')
    }
    assert pgNoSize, 'PatientGroup must reject a grouping that names neither size nor sizeOf'

    // ------------------------------------------------------------------ //
    // ChannelName — the declared name vs the file stem
    // ------------------------------------------------------------------ //
    // A marker has TWO forms and exactly one owner for the mapping between them:
    // the DECLARED name (the samplesheet's spelling, which fills the <marker>
    // slot of the FlowPath measurement key) and the FILE STEM (the sanitised,
    // filesystem-safe form). Identity used to be recovered from the stem, so a
    // declared `HLA.DR` published as `HLA_DR: Cell: Median`.
    //
    // THE TABLE BELOW IS SHARED WITH PYTHON. bin/utils/channel_name.py is the
    // standalone/OME-metadata half of the same rule and tests/test_channel_identity.py
    // holds it to `SANITISER_TABLE` — the same eight rows, same answers. Add a row
    // here and there, or the two halves start drifting again.
    assert ChannelName.fileStem('DAPI')          == 'DAPI'
    assert ChannelName.fileStem('HLA.DR')        == 'HLA_DR'
    assert ChannelName.fileStem('CD3-105')       == 'CD3-105'
    assert ChannelName.fileStem('CD8_beta')      == 'CD8_beta'
    assert ChannelName.fileStem('Ki-67')         == 'Ki-67'
    assert ChannelName.fileStem('CD3 alpha')     == 'CD3_alpha'
    assert ChannelName.fileStem('pS6(240/244)')  == 'pS6_240_244_'
    assert ChannelName.fileStem('\u03b2-catenin')   == '_-catenin'

    // Collisions are numbered by POSITION IN THE DECLARED LIST, not by what is
    // already on disk. Disk-order numbering gave a different answer on a reference
    // slide (nuclear channel kept) than on a moving slide (nuclear channel dropped),
    // and a different answer again in the stub, which writes a different set of files.
    assert ChannelName.fileStems(['CD3.105', 'CD3-105', 'CD3_105']) ==
           ['CD3_105', 'CD3-105', 'CD3_105_2']
    assert ChannelName.fileStems(['DAPI', 'CD3.105', 'CD3_105']) ==
           ['DAPI', 'CD3_105', 'CD3_105_2']
    assert ChannelName.fileStems([]) == []

    // The reverse lookup quantify_markers.nf uses instead of `tiff.baseName`.
    assert ChannelName.declaredFor('HLA_DR', ['DAPI', 'HLA.DR']) == 'HLA.DR'
    assert ChannelName.declaredFor('DAPI',   ['DAPI', 'HLA.DR']) == 'DAPI'
    // Disambiguated collisions resolve back to the RIGHT declared name, not the first.
    assert ChannelName.declaredFor('CD3_105_2', ['CD3.105', 'CD3-105', 'CD3_105']) == 'CD3_105'
    // No declared list (ADD_CYCLE's SPLIT_PRIOR_PYRAMID reads names from OME-XML in
    // REAL mode only, so meta.channels is empty there): fall back to the stem rather
    // than throwing. The stem is still the best name available, and it is what the
    // old code used unconditionally.
    assert ChannelName.declaredFor('HLA_DR', [])   == 'HLA_DR'
    assert ChannelName.declaredFor('HLA_DR', null) == 'HLA_DR'
    // A stem that matches nothing declared is likewise returned unchanged.
    assert ChannelName.declaredFor('CD8', ['DAPI', 'HLA.DR']) == 'CD8'

    // outputStems mirrors MarkerUtils.splitOutputChannels — same nuclear rule, one
    // owner — but returns STEMS, which is what SPLIT_CHANNELS' stub must touch.
    assert ChannelName.outputStems(['DAPI', 'HLA.DR'], true,  ['DAPI']) == ['DAPI', 'HLA_DR']
    assert ChannelName.outputStems(['DAPI', 'HLA.DR'], false, ['DAPI']) == ['HLA_DR']
    assert ChannelName.outputStems([], false, ['DAPI']) == []

    // THE stub-vs-script equality, asserted directly rather than inferred from two
    // nf-tests that happen to expect the same literal.
    //
    // modules/local/split_channels.nf's `script:` hands Python
    // `ChannelName.fileStems(meta.channels)`; its `stub:` touches
    // `ChannelName.outputStems(meta.channels, is_reference, markers)`. The two paths
    // agree iff outputStems is exactly fileStems with the dropped channels removed --
    // never a re-sanitisation, never a different collision suffix. Assert that
    // relation over a channel list carrying every awkward shape at once: a nuclear
    // channel, a dot, a space, and a collision pair.
    def probeChannels = ['DAPI', 'HLA.DR', 'CD3 alpha', 'CD3.105', 'CD3_105']
    def probeStems    = ChannelName.fileStems(probeChannels)
    assert probeStems == ['DAPI', 'HLA_DR', 'CD3_alpha', 'CD3_105', 'CD3_105_2']
    // Reference slide: every declared channel is written, so the stub's list IS
    // the script's list, in order.
    assert ChannelName.outputStems(probeChannels, true, ['DAPI']) == probeStems
    // Moving slide: the nuclear channel is dropped and NOTHING ELSE MOVES --
    // in particular the collision suffix on 'CD3_105' does not shift, which is
    // what an os.path.exists-style numbering could not guarantee.
    assert ChannelName.outputStems(probeChannels, false, ['DAPI']) ==
           probeStems.findAll { it != 'DAPI' }
    // Stated as the general relation, not as three literals: whatever is emitted is
    // a subsequence of the full stem list, never a re-derived spelling.
    [true, false].each { isRef ->
        def emitted = ChannelName.outputStems(probeChannels, isRef, ['DAPI'])
        assert emitted == probeStems.findAll { it in emitted },
               "outputStems(${isRef}) is not a subsequence of fileStems: ${emitted}"
    }

    // shellQuote is what stops `--channels ${meta.channels.join(' ')}` word-splitting a
    // marker whose name contains a space. It must survive a single quote too, since a
    // naive "'" + it + "'" would end the quoted string and hand bash the rest.
    assert ChannelName.shellQuote('CD3 alpha') == "'CD3 alpha'"
    assert ChannelName.shellQuote("O'Brien")   == "'O'\\''Brien'"
    assert ChannelName.shellList(['DAPI', 'CD3 alpha']) == "'DAPI' 'CD3 alpha'"
    assert ChannelName.shellList([]) == ''

    // ------------------------------------------------------------------ //
    // AdapterContract.methodOf -- the seam's type check
    // ------------------------------------------------------------------ //
    // SEG_QC's last argument USED to be the backend name and is now the contract.
    // Groovy will not notice the difference: `contract.method` on a String threw
    // `No such variable: method` from seg_qc.nf:147, a hundred lines past the caller
    // that got it wrong, after a clean git merge combined a test written against the
    // old signature with the new one. methodOf is where that is refused instead.
    assert AdapterContract.methodOf(AdapterContract.of('valis')) == 'valis'
    assert AdapterContract.methodOf(AdapterContract.of('tiled')) == 'tiled'

    def bareNameRejected = false
    try {
        AdapterContract.methodOf('valis')
    }
    catch (IllegalArgumentException e) {
        bareNameRejected = true
        // The message must carry the FIX, not just the complaint: the caller's next
        // action is to wrap the very name they passed.
        assert e.message.contains("AdapterContract.of('valis')"),
            "the refusal must name the one-line fix: ${e.message}"
    }
    assert bareNameRejected, 'methodOf must refuse a bare method name'

    def nullRejected = false
    try { AdapterContract.methodOf(null) }
    catch (IllegalArgumentException e) { nullRejected = true }
    assert nullRejected, 'methodOf must refuse null'

    // A Map is not enough — a half-built contract (no `emits`) would reach the join
    // and answer nothing, which is the failure the cardinality table exists to prevent.
    def halfContractRejected = false
    try { AdapterContract.methodOf([method: 'valis']) }
    catch (IllegalArgumentException e) { halfContractRejected = true }
    assert halfContractRejected, 'methodOf must refuse a map that is not a contract'

    // The other entry point consumers use gets the same refusal, and still answers
    // correctly for a real contract — the two shipped backends disagree here, which is
    // the whole reason seg_qc.nf asks.
    def perSlideRejected = false
    try { AdapterContract.isPerSlide('tiled', 'transform') }
    catch (IllegalArgumentException e) { perSlideRejected = true }
    assert perSlideRejected, 'isPerSlide must refuse a bare method name too'
    assert AdapterContract.isPerSlide(AdapterContract.of('tiled'), 'transform')
    assert !AdapterContract.isPerSlide(AdapterContract.of('valis'), 'transform')

    // ------------------------------------------------------------------ //
    // PanelSignature -- the within-patient slide identity
    // ------------------------------------------------------------------ //
    // In this pipeline a slide's identity WITHIN a patient is its channel set. Both
    // registration backends key on it and they used to disagree about what a repeat
    // means: VALIS_ADAPTER threw its own bespoke message, while SEG_QC's per-slide arm
    // used the same signature as a combine() key and silently cross-produced. One owner,
    // one answer.

    // Order-independent and case-insensitive: the same panel declared two ways is one
    // signature. (A samplesheet's `channels` column is author-ordered; nothing downstream
    // treats that order as meaning anything.)
    assert PanelSignature.of([patient_id: 'P1', channels: ['CD3', 'DAPI']]) ==
           PanelSignature.of([patient_id: 'P1', channels: ['dapi', 'cd3']])
    // ...and a different panel is a different signature.
    assert PanelSignature.of([patient_id: 'P1', channels: ['CD3', 'DAPI']]) !=
           PanelSignature.of([patient_id: 'P1', channels: ['CD8', 'DAPI']])

    // The OME side of VALIS's demux hands it a bare channel-name list read out of the
    // registered file, never a meta. Both must land on the same string or the demux
    // matches nothing.
    assert PanelSignature.ofChannels(['DAPI', 'CD3']) ==
           PanelSignature.of([patient_id: 'P1', channels: ['CD3', 'DAPI']])

    // MUST NOT MUTATE meta.channels. `meta + [k: v]` is clone-then-putAll, so every meta
    // derived from another SHARES the same channels List reference; an in-place sort()
    // here would silently reorder that list for every holder. This is the exact hazard
    // subworkflows/local/adapters/valis_adapter.nf's toSorted() comment describes.
    def sharedChannels = ['PANCK', 'DAPI', 'CD3']
    PanelSignature.of([patient_id: 'P1', channels: sharedChannels])
    assert sharedChannels == ['PANCK', 'DAPI', 'CD3'], 'PanelSignature.of mutated its input'

    // Distinct panels within a patient: accepted, silently.
    PanelSignature.requireUniqueWithinPatient('P001', [
        [patient_id: 'P001', id: 'P001_ref',  channels: ['DAPI', 'PANCK']],
        [patient_id: 'P001', id: 'P001_mov1', channels: ['DAPI', 'CD3']],
    ])

    // A repeated panel is REFUSED, and the message has to be actionable: it must name the
    // patient, the duplicated panel, and the ids of the slides that collide. The reason it
    // is refused rather than accepted is downstream, not here -- every artifact past
    // registration (split channel TIFF, per-marker quantification column, merged CSV) is
    // keyed by MARKER NAME, so two slides declaring the same markers overwrite each other
    // at quantification whichever backend registered them.
    def dupRejected = false
    try {
        PanelSignature.requireUniqueWithinPatient('P001', [
            [patient_id: 'P001', id: 'P001_cyc1', channels: ['DAPI', 'CD3']],
            [patient_id: 'P001', id: 'P001_cyc2', channels: ['CD3', 'DAPI']],
        ])
    }
    catch (IllegalArgumentException e) {
        dupRejected = true
        assert e.message.contains('P001')
        assert e.message.contains('P001_cyc1') && e.message.contains('P001_cyc2'),
            "the message must name the colliding slides: ${e.message}"
        assert e.message.toLowerCase().contains('cd3'),
            "the message must name the duplicated panel: ${e.message}"
    }
    assert dupRejected, 'PanelSignature.requireUniqueWithinPatient must reject a repeated panel'

    // A single slide can never collide with itself.
    PanelSignature.requireUniqueWithinPatient('P002', [
        [patient_id: 'P002', id: 'P002_ref', channels: ['DAPI', 'PANCK']],
    ])

    // ---- POST-REBIND metas, which is what the call sites actually judge ----
    //
    // Every assertion above hand-builds its meta. Neither call site sees one of those:
    // requireUniqueWithinPatient is called on ch_grouped_multi
    // (subworkflows/local/register_patient.nf) and again at VALIS_ADAPTER's demux, and
    // by then `channels` has been REBOUND by subworkflows/local/preprocess.nf to the
    // list read back out of CONVERT_IMAGE's <prefix>_channels.txt. A review read that
    // rebind as DROPPING the nuclear channel, which would make two slides differing
    // only in their nuclear marker collide here and abort a legitimate patient.
    //
    // It does not drop it: bin/convert_image.py MOVES the nuclear channel to index 0
    // (utils.metadata.nuclear_first), a permutation of the declared list, and
    // tests/test_panel_signature_survives_preprocess.py pins that it never filters.
    // This block is the Groovy half of the same fact -- pytest cannot load lib/.
    //
    // `rebind` is preprocess.nf's expression verbatim, applied to the channels-file
    // body convert_image.py writes for each slide, so these metas have the shape the
    // check receives rather than the shape a test author would type.
    def rebind = { Map declaredMeta, String channelsFileText ->
        declaredMeta + [channels: channelsFileText.trim().split(',').toList()]
    }
    def slideDapi    = rebind([patient_id: 'P003', id: 'P003_dapi',    channels: ['CD3', 'CD8', 'DAPI']],    'DAPI,CD3,CD8')
    def slideCelltox = rebind([patient_id: 'P003', id: 'P003_celltox', channels: ['CD3', 'CD8', 'CELLTOX']], 'CELLTOX,CD3,CD8')

    // The rebind reorders and nothing else, so the signature is the DECLARED one.
    assert PanelSignature.of(slideDapi) ==
           PanelSignature.ofChannels(['CD3', 'CD8', 'DAPI']),
        'the post-rebind signature must equal the declared one'
    assert slideDapi.channels.size() == 3,
        "the rebind dropped a channel: ${slideDapi.channels}"

    // ...so the pair the review was worried about is ACCEPTED, as it must be: these are
    // two different panels and the run they belong to used to complete.
    PanelSignature.requireUniqueWithinPatient('P003', [slideDapi, slideCelltox])

    // ...and the check is still LIVE on rebound metas -- a genuine repeat, post-rebind,
    // is still refused. Without this the accept above could pass because the check had
    // quietly stopped judging anything.
    def rebindDupRejected = false
    try {
        PanelSignature.requireUniqueWithinPatient('P004', [
            rebind([patient_id: 'P004', id: 'P004_c1', channels: ['CD3', 'DAPI']], 'DAPI,CD3'),
            rebind([patient_id: 'P004', id: 'P004_c2', channels: ['DAPI', 'CD3']], 'DAPI,CD3'),
        ])
    }
    catch (IllegalArgumentException e) {
        rebindDupRejected = true
        assert e.message.contains('P004_c1') && e.message.contains('P004_c2')
    }
    assert rebindDupRejected,
        'requireUniqueWithinPatient must still refuse a repeated panel on post-rebind metas'

    println "LIB PROBE: all assertions passed"
}
