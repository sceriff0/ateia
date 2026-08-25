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


    // ------------------------------------------------------------------ //
    // CsvUtils.resolveKeptChannelsPerSlide — THE keep-set rule
    // ------------------------------------------------------------------ //
    // Each marker name is claimed exactly once per patient, reference first then
    // samplesheet order. That single invariant does two things. It makes the winner of a
    // shared marker DETERMINISTIC -- two slides carrying the same name used to be
    // deduplicated by ARRIVAL ORDER at a downstream .unique(), so which copy reached
    // merged_quant.csv and the pyramid varied run to run. And it collapses the
    // distinct-name count and the emitted-file count into ONE number, which is what lets
    // a single channels_count size every downstream groupKey correctly.
    //
    // It is NOT that some consumer needs the file count: both groupTiffsByPatient callers
    // dedup on [patient_id, marker] immediately upstream (postprocess.nf's `.unique`,
    // add_cycle.nf's priority groupTuple), so the distinct-name count would serve them
    // today. An earlier version of this comment said otherwise.
    def keepCsv = File.createTempFile('keepset', '.csv')
    keepCsv.text = '''patient_id,image,channels,is_reference
P1,ref.tiff,DAPI|KI67|CD20,true
P1,cyc2.tiff,CELLTOX|CD8,false
P1,cyc3.tiff,CELLTOX|FOXP3,false
'''
    def keep = CsvUtils.resolveKeptChannelsPerSlide(keepCsv.path, 'image', ['DAPI','CELLTOX'], false)
    assert keep['P1']['ref.tiff']  == ['DAPI', 'KI67', 'CD20']
    assert keep['P1']['cyc2.tiff'] == ['CELLTOX', 'CD8']  // CELLTOX unclaimed -> KEPT
    assert keep['P1']['cyc3.tiff'] == ['FOXP3']           // CELLTOX claimed by cyc2
    def keepFlat = keep['P1'].values().flatten()
    assert keepFlat.size() == keepFlat.toSet().size()
    assert keepFlat.size() == 6
    keepCsv.delete()

    // The reference wins wherever it sits in the sheet: ordering is reference-first,
    // not row-order, so a reference declared LAST still claims its markers first.
    def keepLateRef = File.createTempFile('keeplate', '.csv')
    keepLateRef.text = '''patient_id,image,channels,is_reference
P2,cyc2.tiff,DAPI|CD8,false
P2,ref.tiff,DAPI|KI67,true
'''
    def lateRef = CsvUtils.resolveKeptChannelsPerSlide(keepLateRef.path, 'image', ['DAPI','CELLTOX'], false)
    assert lateRef['P2']['ref.tiff']  == ['DAPI', 'KI67']
    assert lateRef['P2']['cyc2.tiff'] == ['CD8']   // DAPI already claimed by the reference
    keepLateRef.delete()

    // preClaimed seeds the claimed set — add_cycle passes the prior run's reference
    // channels, so a re-stained DAPI is redundant but a NEW nuclear marker survives.
    def keepPrior = File.createTempFile('keepprior', '.csv')
    keepPrior.text = '''patient_id,image,channels,is_reference
P3,cyc4.tiff,DAPI|CELLTOX|CD8,false
'''
    def seeded = CsvUtils.resolveKeptChannelsPerSlide(
        keepPrior.path, 'image', ['DAPI','CELLTOX'], false, ['P3': ['DAPI', 'KI67']])
    assert seeded['P3']['cyc4.tiff'] == ['CELLTOX', 'CD8']  // DAPI pre-claimed; CELLTOX new
    keepPrior.delete()

    // BASENAME IS NOT A KEY. Two rows of one patient can share an image BASENAME while
    // living in different directories -- a cyclic-IF cohort with one directory per cycle
    // is the ordinary case, not a pathological one. The inner map is therefore keyed on
    // the RAW image cell, exactly what resolveReferenceRows returns and exactly what
    // input_check.nf's meta.is_reference already compares against. Keyed on the basename
    // the two rows overwrote each other: the map held ONE entry, both rows looked it up,
    // and the reference was handed the other slide's keep-set -- in a real run it emitted
    // ZERO channels, DAPI included, while the count read 1 instead of 3.
    def dupCsv = File.createTempFile('keepdup', '.csv')
    dupCsv.text = '''patient_id,image,channels,is_reference
P4,/data/c1/slide.tiff,DAPI|CD3,true
P4,/data/c2/slide.tiff,DAPI|CD8,false
'''
    def dup = CsvUtils.resolveKeptChannelsPerSlide(dupCsv.path, 'image', ['DAPI','CELLTOX'], false)
    assert dup['P4'].size() == 2                              // two rows, two entries
    assert dup['P4']['/data/c1/slide.tiff'] == ['DAPI', 'CD3']  // the reference claims DAPI
    assert dup['P4']['/data/c2/slide.tiff'] == ['CD8']          // DAPI already claimed
    assert CsvUtils.countChannelsPerPatient(dupCsv.path, 'image', ['DAPI','CELLTOX'], false)['P4'] == 3
    dupCsv.delete()

    // AN EMPTY KEEP-SET IS AN ANSWER, NOT AN ABSENCE. A slide whose every declared
    // channel was already claimed contributes NO new markers, and countChannelsPerPatient
    // counts it as contributing ZERO. Its entry must therefore be PRESENT and EMPTY:
    // consumers have to be able to tell "this slide emits nothing" (emit nothing) from
    // "this slide has no entry" (fall back to its declared list). Groovy's `?:` cannot --
    // it treats [] as falsy -- so every lookup of this map uses containsKey/an explicit
    // null test, and input_check.nf does the lookup where the raw cell is in scope.
    // Resolving the empty entry to the FULL declared list emitted a duplicate marker NAME
    // across two slides of one patient, which is exactly what the one-name-per-patient
    // invariant exists to forbid.
    def emptyCsv = File.createTempFile('keepempty', '.csv')
    emptyCsv.text = '''patient_id,image,channels,is_reference
P5,ref.tiff,DAPI|PANCK|SMA,true
P5,mov1.tiff,DAPI,false
'''
    def emptyKeep = CsvUtils.resolveKeptChannelsPerSlide(emptyCsv.path, 'image', ['DAPI','CELLTOX'], false)
    assert emptyKeep['P5'].containsKey('mov1.tiff')   // present ...
    assert emptyKeep['P5']['mov1.tiff'] == []         // ... and EMPTY, not the declared list
    assert emptyKeep['P5']['ref.tiff'] == ['DAPI', 'PANCK', 'SMA']
    assert CsvUtils.countChannelsPerPatient(emptyCsv.path, 'image', ['DAPI','CELLTOX'], false)['P5'] == 3
    emptyCsv.delete()

    // THE invariant countChannelsPerPatient exists to satisfy: the count equals BOTH the
    // number of TIFFs emitted AND the number of distinct marker names, because the
    // one-name-per-patient rule makes those the same number. Pinning both equalities here
    // is what stops a future change to the resolver from quietly making channels_count
    // right for one downstream grouping and wrong for the other.
    def invCsv = File.createTempFile('keepcount', '.csv')
    invCsv.text = '''patient_id,image,channels,is_reference
P1,ref.tiff,DAPI|KI67|CD20,true
P1,cyc2.tiff,CELLTOX|CD8,false
P1,cyc3.tiff,CELLTOX|FOXP3,false
'''
    def invCounts = CsvUtils.countChannelsPerPatient(invCsv.path, 'image', ['DAPI','CELLTOX'], false)
    def invKept   = CsvUtils.resolveKeptChannelsPerSlide(invCsv.path, 'image', ['DAPI','CELLTOX'], false)
    def invFlat   = invKept['P1'].values().flatten()
    assert invCounts['P1'] == 6
    assert invCounts['P1'] == invFlat.size()          // == emitted TIFF count (pyramid)
    assert invCounts['P1'] == invFlat.toSet().size()  // == distinct names     (quant)
    invCsv.delete()

    // ------------------------------------------------------------------ //
    // ParamUtils — the step vocabulary
    // ------------------------------------------------------------------ //
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
        assert step.entryColumn in step.requiredColumns,
            "step '${step.name}': entryColumn '${step.entryColumn}' not in requiredColumns"
    }

    // ------------------------------------------------------------------ //
    // Checkpoint — filename AND columns, one owner
    // ------------------------------------------------------------------ //
    assert Checkpoint.columns(Layout.PREPROCESSED) ==
        ['patient_id', 'id', 'preprocessed_image', 'is_reference', 'channels']
    assert Checkpoint.columns(Layout.REGISTERED) ==
        ['patient_id', 'id', 'registered_image', 'is_reference', 'channels']
    assert Checkpoint.columns(Layout.POSTPROCESSED) ==
        ['patient_id', 'id', 'cell_csv', 'cell_geojson', 'merged_csv', 'cell_mask', 'pyramid']

    // The header IS the seed: string the three writers pass to collectFile. These
    // three literals are the published contract — Group A must not change them.
    // RULING R17 (Task 4.3) added 'id' right after 'patient_id' to all four.
    assert Checkpoint.header(Layout.PREPROCESSED)  == 'patient_id,id,preprocessed_image,is_reference,channels'
    assert Checkpoint.header(Layout.REGISTERED)    == 'patient_id,id,registered_image,is_reference,channels'
    assert Checkpoint.header(Layout.POSTPROCESSED) == 'patient_id,id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid'

    // row() emits values in DECLARED COLUMN ORDER regardless of map insertion order.
    // This is the whole point: a writer can no longer transpose two columns.
    assert Checkpoint.row(Layout.REGISTERED, [
        channels: 'DAPI|CD3', patient_id: 'P001', id: 'P001_x',
        registered_image: '/out/P001/registered/x.ome.tiff', is_reference: false
    ]) == 'P001,P001_x,/out/P001/registered/x.ome.tiff,false,DAPI|CD3'

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
            patient_id: 'P001', id: 'P001_x', registered_image: '/x', is_reference: false,
            channels: 'DAPI', typo_column: 'oops'
        ])
    }
    catch (IllegalArgumentException ignored) { unknownCol = true }
    assert unknownCol, 'Checkpoint.row must reject an unknown column'

    // Checkpoint and Layout must agree on the step vocabulary. Two tables that drift
    // is the exact failure this extraction exists to prevent.
    assert Checkpoint.STEPS*.name == Layout.CHECKPOINT_STEPS

    // A step's requiredColumns used to EQUAL the previous step's full checkpoint
    // column list -- that was true before RULING R17. It no longer is, by design:
    // 'id' was added to every Checkpoint.STEPS schema (every checkpoint FILE now
    // carries identity), but 'registration'/'segmentation' still enter through
    // INPUT_CHECK (Meta.fromSamplesheetRow), which derives id itself from the entry
    // image column and never reads a persisted 'id' value -- so requiring it in
    // CsvUtils.validateInputCSV there would rightly reject an OLDER checkpoint
    // (preprocessed.csv/registered.csv written before this task) that INPUT_CHECK
    // could actually still read correctly. Only 'postprocessing' actually
    // dereferences 'id' (via READ_SEGMENTED_CHECKPOINT's Meta.fromCheckpointRow),
    // so only IT gained 'id' in requiredColumns (see lib/ParamUtils.groovy's
    // comment on that entry). The invariant is now "every OTHER column" equality,
    // not full-list equality.
    assert ParamUtils.STEPS.find { it.name == 'registration' }.requiredColumns ==
        Checkpoint.columns(Layout.PREPROCESSED) - ['id']
    assert ParamUtils.STEPS.find { it.name == 'segmentation' }.requiredColumns ==
        Checkpoint.columns(Layout.REGISTERED) - ['id']
    assert ParamUtils.STEPS.find { it.name == 'postprocessing' }.requiredColumns ==
        ['patient_id', 'id', 'registered_image', 'is_reference', 'channels', 'cell_mask', 'nuclei_mask']
    assert ParamUtils.STEPS.find { it.name == 'postprocessing' }.requiredColumns
        .every { it in Checkpoint.columns(Layout.SEGMENTED) }

    // columns() must hand out an IMMUTABLE list. Checkpoint.STEPS' outer list is
    // .asImmutable() but a naive implementation leaves each entry's inner `columns`
    // ArrayList mutable, so a caller mutating the returned list permanently corrupts
    // the schema every later header()/row() call in the run sees. Demonstrated before
    // the fix: `Checkpoint.columns(Layout.PREPROCESSED) << 'injected'` succeeded
    // silently and mutated the live table in place, so a subsequent header() call
    // returned 'patient_id,id,preprocessed_image,is_reference,channels,injected'.
    def columnsThrew = false
    try { Checkpoint.columns(Layout.PREPROCESSED) << 'injected' }
    catch (UnsupportedOperationException ignored) { columnsThrew = true }
    assert columnsThrew, 'Checkpoint.columns() must return an immutable list; mutating it must throw'
    assert Checkpoint.columns(Layout.PREPROCESSED) ==
        ['patient_id', 'id', 'preprocessed_image', 'is_reference', 'channels'],
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
        'patient_id', 'id', 'registered_image', 'is_reference', 'channels',
        'cell_mask', 'nuclei_mask', 'contours', 'nucleus_contours',
    ]
    assert Checkpoint.header(Layout.SEGMENTED) ==
        'patient_id,id,registered_image,is_reference,channels,cell_mask,nuclei_mask,contours,nucleus_contours'

    // Empty string means "artifact not produced" — nucleus_contours is empty when
    // --quantify_compartments is false (nuclei_mask is NOT similarly gated: SEGMENT
    // always produces it — see Checkpoint's EMPTY VALUES note). row() must accept ''
    // (it is a value, not a missing key) and emit it as an empty field regardless of
    // which column carries it.
    assert Checkpoint.row(Layout.SEGMENTED, [
        patient_id: 'P001', id: 'P001_a', registered_image: '/o/P001/registered/a.tif',
        is_reference: true, channels: 'DAPI|CD3',
        cell_mask: '/o/P001/segmentation/P001_cell_mask.tif',
        nuclei_mask: '/o/P001/segmentation/P001_nuclei_mask.tif',
        contours: '/o/P001/cell_properties/contours.json',
        nucleus_contours: '',
    ]) == 'P001,P001_a,/o/P001/registered/a.tif,true,DAPI|CD3,/o/P001/segmentation/P001_cell_mask.tif,/o/P001/segmentation/P001_nuclei_mask.tif,/o/P001/cell_properties/contours.json,'

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
    // ParamUtils.compartmentMode / validateCompartmentQuant -- the
    // --quantify_compartments seam (mirrors --registration_method's: resolved
    // once, threaded down as an argument; see workflows/mirage.nf's
    // `compartment_mode` and tests/test_compartment_mode_routing.py).
    // ------------------------------------------------------------------ //

    // 1. Plain field mapping, all three flags on.
    def modeAllOn = ParamUtils.compartmentMode([
        quantify_compartments: true, expanded_quantification: true, embed_masks: true,
    ])
    assert modeAllOn == [compartments: true, expanded: true, embedMasks: true]

    // 2. All three off.
    def modeAllOff = ParamUtils.compartmentMode([
        quantify_compartments: false, expanded_quantification: false, embed_masks: false,
    ])
    assert modeAllOff == [compartments: false, expanded: false, embedMasks: false]

    // 3. The map is immutable -- a caller cannot mutate the shared snapshot out
    // from under another reader of the same resolved value.
    try {
        modeAllOn.compartments = false
        assert false : "compartmentMode() map must be immutable"
    } catch (UnsupportedOperationException ignored) {
        // expected
    }

    // 4. validateCompartmentQuant: expanded requires compartments (pre-existing
    // rule, now driven off the resolved map instead of two raw booleans).
    ParamUtils.validateCompartmentQuant([compartments: true, expanded: true, embedMasks: false])   // ok
    ParamUtils.validateCompartmentQuant([compartments: true, expanded: false, embedMasks: false])  // ok
    try {
        ParamUtils.validateCompartmentQuant([compartments: false, expanded: true, embedMasks: false])
        assert false : "expanded=true, compartments=false must be rejected"
    } catch (IllegalArgumentException ignored) {
        // expected
    }

    // 5. validateCompartmentQuant: the delayed-cost gap this task closes --
    // embedMasks requires BOTH compartments AND expanded. Each way to violate
    // that must be rejected, not just the case where compartments is off.
    ParamUtils.validateCompartmentQuant([compartments: true, expanded: true, embedMasks: true])    // ok
    try {
        ParamUtils.validateCompartmentQuant([compartments: false, expanded: false, embedMasks: true])
        assert false : "embedMasks=true with compartments=false, expanded=false must be rejected"
    } catch (IllegalArgumentException ignored) {
        // expected -- this exact combination used to exit 0 and silently publish
        // a pyramid with no mask series (assemble_export.nf's embed_masks gate).
    }
    try {
        ParamUtils.validateCompartmentQuant([compartments: true, expanded: false, embedMasks: true])
        assert false : "embedMasks=true with expanded=false must be rejected even when compartments=true"
    } catch (IllegalArgumentException ignored) {
        // expected
    }

    // ------------------------------------------------------------------ //
    // Layout — the published-kind vocabulary
    // ------------------------------------------------------------------ //
    assert Layout.requireKind('segmentation') == 'segmentation'
    assert Layout.PUBLISHED_KINDS.contains(Layout.REGISTERED)
    assert Layout.PUBLISHED_KINDS.contains('split_channels')

    def badKind = false
    try { Layout.requireKind('segmentaton') }   // typo, deliberately
    catch (IllegalArgumentException ignored) { badKind = true }
    assert badKind, 'Layout.requireKind must reject an unknown kind'

    // patientDir must reject it too — that is the call site the typo reaches from.
    def badPatientKind = false
    try { Layout.patientDir('/out', 'P001', 'segmentaton') }
    catch (IllegalArgumentException ignored) { badPatientKind = true }
    assert badPatientKind, 'Layout.patientDir must reject an unknown kind'

    // ------------------------------------------------------------------ //
    // ProcessEnvelope — the versions.yml envelope
    // ------------------------------------------------------------------ //
    def envVersions     = ProcessEnvelope.versions('TEST:PROC', ['numpy', 'skimage'])
    def envVersionsStub = ProcessEnvelope.versionsStub('TEST:PROC', ['numpy', 'skimage'])

    // The python: row is prepended automatically, in both renderings. A BARE `$(`, not
    // `\$(` -- `<<-END_VERSIONS` is an unquoted heredoc, so bash performs command
    // substitution on the former and prints the latter as literal text. This assertion
    // is the one that would have caught the over-escaping bug: it asserted `\\$(` (an
    // escaped, unexecuted dollar) until a reviewer caught that the real published
    // versions.yml was showing shell commands instead of version numbers.
    assert envVersions.contains('python: $(python --version')
    assert !envVersions.contains('python: \\$(python --version')
    assert envVersionsStub.contains('python: stub')

    // skimage -> scikit-image: the import name and the published YAML key differ, and
    // bin/generate_qc_report.py's hand-rolled parser is keyed on the YAML key, not the
    // import name.
    assert envVersions.contains('scikit-image: $(python -c "import skimage;')
    assert !envVersions.contains('skimage: $(python -c "import skimage;')
    assert envVersionsStub.contains('scikit-image: stub')

    // The property this whole task exists to guarantee: versions() and versionsStub()
    // must never be able to name a different set of tools. Comparing the two heredocs'
    // YAML KEYS (the text before each ':') is what -stub could never see for itself,
    // because -stub never evaluates the script: block that versions() renders.
    def keysOf = { String heredoc ->
        heredoc.readLines()
            .findAll { it.startsWith('    ') }
            .collect { it.trim().split(':')[0].trim() }
            .toSet()
    }
    assert keysOf(envVersions) == keysOf(envVersionsStub)
    assert keysOf(envVersions) == ['python', 'numpy', 'scikit-image'].toSet()

    // ------------------------------------------------------------------ //
    // SegBackends — the segmentation seam's versions.yml tool lists
    // ------------------------------------------------------------------ //
    // Declared as BARE MODULE NAMES, not rendered YAML. That is what lets
    // modules/local/segment.nf hand the SAME list to ProcessEnvelope.versions()
    // (script:) and ProcessEnvelope.versionsStub() (stub:). Before this, the table
    // stored six pre-rendered probe strings, which a stub block cannot reuse -- so
    // segment.nf's stub: hand-wrote `seg_method: <name>` instead, a key that is not a
    // tool and that no real run ever emits. `-stub` never evaluates script:, so nothing
    // in the blocking gate could see the two key sets were disjoint.
    assert SegBackends.methods().toSorted() == ['cellsam', 'instantseg', 'stardist']
    assert SegBackends.of('stardist').versionTools   == ['deepcell', 'tensorflow']
    assert SegBackends.of('instantseg').versionTools == ['instanseg', 'torch']
    assert SegBackends.of('cellsam').versionTools    == ['cellSAM', 'torch']

    // `torch` is deliberately repeated across two backends rather than hoisted into a
    // shared list: exactly ONE backend runs per task, and stardist loads no torch at
    // all, so a shared list would fabricate a torch row for every StarDist run.
    assert !SegBackends.of('stardist').versionTools.contains('torch')

    // The property the whole envelope exists for, asserted end-to-end for every
    // backend: rendering the SAME list through versions() and versionsStub() yields
    // the SAME YAML keys. Compare keys (the text before each ':'), because the values
    // are exactly what differs between a real run and a stub.
    def yamlKeysOf = { String heredoc ->
        heredoc.readLines()
            .findAll { it.startsWith('    ') }
            .collect { it.trim().split(':')[0].trim() }
    }
    SegBackends.methods().each { m ->
        def tools = SegBackends.of(m).versionTools
        def real  = yamlKeysOf(ProcessEnvelope.versions('SEGMENT', tools))
        def stub  = yamlKeysOf(ProcessEnvelope.versionsStub('SEGMENT', tools))
        assert real == stub, "SEGMENT/${m}: script: and stub: versions.yml keys differ"
        assert real == ['python'] + tools, "SEGMENT/${m}: unexpected versions.yml keys ${real}"
        // The old stub key must not come back under any backend.
        assert !real.contains('seg_method')
    }

    // ------------------------------------------------------------------ //
    // WarpBackends — one seam for the reg_qc=2 warp
    // ------------------------------------------------------------------ //
    assert WarpBackends.methods().toSorted() == ['ashlar', 'tiled', 'valis']
    assert WarpBackends.container('valis') == 'cdgatenbee/valis-wsi:1.0.0'
    assert WarpBackends.container('tiled') == 'bolt3x/mirage-tiled:1.0.0'
    assert WarpBackends.of('valis').stages == ['native', 'rigid', 'non_rigid', 'micro']
    assert WarpBackends.of('tiled').stages == ['native', 'rigid', 'refined']

    // ashlar shares the tiled container AND the tiled stage vocabulary, because
    // bin/ashlar_solve.py emits the identical M0 + mesh manifest: the scorer reads it
    // through the same JVM-free warper, so it needs neither the ashlar image nor VALIS.
    assert WarpBackends.container('ashlar') == 'bolt3x/mirage-tiled:1.0.0'
    assert WarpBackends.of('ashlar').stages == ['native', 'rigid', 'refined']
    assert WarpBackends.of('ashlar').versionTools == WarpBackends.of('tiled').versionTools

    // The tiled backend must pass --method tiled; VALIS must not.
    assert WarpBackends.of('tiled').flags([:]).any { it.contains('--method tiled') }
    // ashlar passes its OWN method name, not 'tiled'. Both route to the same warper, but
    // the report records what it was given -- an ashlar arm labelled 'tiled' in the QC
    // JSON is indistinguishable from a STARE arm in the table the arm ranking is built
    // from, which is precisely the comparison the backend exists to make.
    assert WarpBackends.of('ashlar').flags([:]).any { it.contains('--method ashlar') }
    assert !WarpBackends.of('ashlar').flags([:]).any { it.contains('--method tiled') }
    // No JVM heap flag may leak onto a JVM-free backend (see the WarpBackends header).
    assert !WarpBackends.of('ashlar').flags([:]).any { it.contains('--jvm-heap-gb') }
    def valisFlags = WarpBackends.of('valis').flags(
        [ref_slide: 'R', moving_slide: 'M', stage_checkpoint: null, micro_reg: 2])
    assert !valisFlags.any { it.contains('--method') }
    assert valisFlags.any { it.contains('--micro-reg 2') }
    // Absent checkpoint is a null object, not an empty flag.
    assert !valisFlags.any { it.contains('--checkpoint-dir') }
    assert WarpBackends.of('valis').flags(
        [ref_slide: 'R', moving_slide: 'M', stage_checkpoint: 'ckpt/', micro_reg: 2]
    ).any { it.contains('--checkpoint-dir ckpt/') }

    def badMethod = false
    try { WarpBackends.of('stare') }
    catch (IllegalArgumentException ignored) { badMethod = true }
    assert badMethod, 'WarpBackends.of must reject an unknown method'

    // ------------------------------------------------------------------ //
    // Meta — the one constructor for every meta map (Task 4.1)
    // ------------------------------------------------------------------ //
    assert Meta.REQUIRED_KEYS.containsAll([
        'patient_id', 'id', 'is_reference', 'channels',
        'keep_channels', 'channels_count', 'images_count',
    ])

    // identityFor: RULING R2, verified directly against Nextflow's own
    // file(...).simpleName earlier in this task -- it strips EVERY extension
    // (file('slide.ome.tiff').simpleName == 'slide'), not just the last one.
    // identityFor must reproduce that for a non-colliding row.
    assert Meta.identityFor('P1', 'slide.ome.tiff', 0, [:]) == 'P1_slide'
    assert Meta.identityFor('P1', 'slide.tiff', 0, [:])     == 'P1_slide'
    assert Meta.identityFor('P1', 'slide.tif', 0, [:])      == 'P1_slide'
    assert Meta.identityFor('P1', 'noext', 0, [:])          == 'P1_noext'
    // Already patient-prefixed: not re-prefixed.
    assert Meta.identityFor('P1', 'P1_slide.ome.tiff', 0, [:]) == 'P1_slide'
    // A collision (stemCounts says two rows of P1 share the 'slide' stem) is
    // disambiguated by rowIndex; a non-colliding stem for a different patient
    // sharing the same stem text is NOT (the counts map is keyed per-patient).
    def collideCtx = [stemCounts: ['P1::slide': 2]]
    assert Meta.identityFor('P1', 'cycle1/slide.ome.tiff', 0, collideCtx) == 'P1_slide_000'
    assert Meta.identityFor('P1', 'cycle2/slide.ome.tiff', 1, collideCtx) == 'P1_slide_001'
    assert Meta.identityFor('P2', 'slide.ome.tiff', 0, collideCtx)        == 'P2_slide'

    // fromSamplesheetRow: full construction, REQUIRED_KEYS present, dual invariant
    // upheld (channels_count comes from ctx.channelsCount, the per-PATIENT total --
    // NOT meta.keep_channels.size(), which is only a per-SLIDE count. Conflating
    // the two was a bug in this task's own brief: CsvUtils.countChannelsPerPatient
    // sums per-slide keep-set sizes ACROSS a patient's slides, so a single slide's
    // keep_channels.size() under-reports it whenever a patient has more than one
    // slide).
    def ssCtx = [
        keepChannelsBySlide: [P1: ['img1.ome.tiff': ['DAPI', 'CD3']]],
        imagesCount        : [P1: 2],
        channelsCount      : [P1: 5],  // e.g. this slide's 2 + a second slide's 3
    ]
    def ssRow  = [patient_id: 'P1', image: 'img1.ome.tiff', channels: 'DAPI|CD3', is_reference: 'true']
    def ssMeta = Meta.fromSamplesheetRow(ssRow, 'image', 0, ssCtx)
    assert Meta.REQUIRED_KEYS.every { ssMeta.containsKey(it) }
    assert ssMeta.patient_id     == 'P1'
    assert ssMeta.id             == 'P1_img1'
    assert ssMeta.is_reference   == true
    assert ssMeta.channels       == ['DAPI', 'CD3']
    assert ssMeta.keep_channels  == ['DAPI', 'CD3']
    assert ssMeta.channels_count == 5
    assert ssMeta.images_count   == 2

    // keep_channels ABSENT vs EMPTY: a slide with no entry in keepChannelsBySlide
    // falls back to its declared channels; a slide with an EXPLICIT empty list
    // (every marker already claimed by an earlier slide) keeps [], not the
    // declared list -- `?:` cannot tell these apart because [] is falsy in Groovy.
    // Both ctx maps below must also carry channelsCount AND imagesCount now (fix
    // rounds 1 and 2: there is no more per-slide/default fallback for either --
    // see the channels_count-missing and images_count-missing blocks further down).
    def emptyKeepCtx = [keepChannelsBySlide: [P1: ['img2.ome.tiff': []]], channelsCount: [P1: 0], imagesCount: [P1: 1]]
    def emptyKeepRow = [patient_id: 'P1', image: 'img2.ome.tiff', channels: 'DAPI|CD3', is_reference: 'false']
    def emptyKeepMeta = Meta.fromSamplesheetRow(emptyKeepRow, 'image', 0, emptyKeepCtx)
    assert emptyKeepMeta.keep_channels == []
    assert emptyKeepMeta.channels_count == 0
    def absentKeepCtx = [keepChannelsBySlide: [P1: [:]], channelsCount: [P1: 2], imagesCount: [P1: 1]]
    def absentKeepMeta = Meta.fromSamplesheetRow(emptyKeepRow, 'image', 0, absentKeepCtx)
    assert absentKeepMeta.keep_channels == ['DAPI', 'CD3']

    // ---- Fix round 1, item 1: channels_count has NO fallback -----------------
    //
    // finish() used to fall back to meta.keep_channels.size() (a per-SLIDE count)
    // when ctx.channelsCount had no entry for the patient. That silently produced
    // a too-low count for any multi-slide patient, and a too-low channels_count
    // feeds groupKey(patient_id, channels_count) -- the group then emits early
    // with missing members, or never emits, far from this call site. There is now
    // no fallback and no opt-out: every caller must pre-compute a real per-patient
    // total (see finish()'s comment for why no legitimate caller lacks one).
    def noCountRow = [patient_id: 'P1', image: 'img3.ome.tiff', channels: 'DAPI|CD3', is_reference: 'false']

    // Trigger: ctx carries no channelsCount key at all.
    def missingChannelsCountKey = false
    try { Meta.fromSamplesheetRow(noCountRow, 'image', 0, [:]) }
    catch (IllegalArgumentException ignored) { missingChannelsCountKey = true }
    assert missingChannelsCountKey, 'Meta.fromSamplesheetRow must reject ctx with no channelsCount map at all'

    // Trigger: ctx.channelsCount is populated, but not for THIS patient (the
    // "computed the map, forgot one patient" case -- the one the review named).
    def missingChannelsCountForPatient = false
    try { Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P2: 9]]) }
    catch (IllegalArgumentException ignored) { missingChannelsCountForPatient = true }
    assert missingChannelsCountForPatient, 'Meta.fromSamplesheetRow must reject ctx.channelsCount missing THIS patient'

    // Satisfy: supply BOTH required per-patient counts, watch it pass.
    def satisfiedMeta = Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P1: 2], imagesCount: [P1: 1]])
    assert satisfiedMeta.channels_count == 2

    // A GENUINE zero is not "missing": containsKey, not a truthy/`?:` check, is
    // what tells these apart -- the same distinction keep_channels' ABSENT-vs-EMPTY
    // rule already has to make.
    def zeroCountMeta = Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P1: 0], imagesCount: [P1: 1]])
    assert zeroCountMeta.channels_count == 0

    // ---- Fix round 2: images_count has NO fallback either --------------------
    //
    // Same defect, one field over, and WORSE while it lasted: the old expression
    // was `(ctx?.imagesCount ?: [:])[patientId] ?: 1` -- a bare `?:` on the looked-up
    // value, which in Groovy treats an explicit `0` as falsy too. So a genuine
    // `images_count: 0` would have been silently COERCED to `1`, not merely
    // defaulted to a differently-wrong number the way the old channels_count bug
    // was. images_count is equally load-bearing: it feeds
    // groupKey(meta.patient_id, meta.images_count) in
    // subworkflows/local/registration.nf:77. Same fix, same helper
    // (requirePerPatientCount), same reasoning: no opt-out, because every caller
    // already computes a full per-patient imagesCount map up front
    // (CsvUtils.countImagesPerPatient).

    // Trigger: ctx carries no imagesCount key at all (channelsCount present, so
    // this isolates the images_count check specifically).
    def missingImagesCountKey = false
    try { Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P1: 2]]) }
    catch (IllegalArgumentException ignored) { missingImagesCountKey = true }
    assert missingImagesCountKey, 'Meta.fromSamplesheetRow must reject ctx with no imagesCount map at all'

    // Trigger: ctx.imagesCount is populated, but not for THIS patient.
    def missingImagesCountForPatient = false
    try { Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P1: 2], imagesCount: [P2: 9]]) }
    catch (IllegalArgumentException ignored) { missingImagesCountForPatient = true }
    assert missingImagesCountForPatient, 'Meta.fromSamplesheetRow must reject ctx.imagesCount missing THIS patient'

    // Satisfy: supply the entry, watch it pass.
    def satisfiedImagesMeta = Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P1: 2], imagesCount: [P1: 3]])
    assert satisfiedImagesMeta.images_count == 3

    // THE bug this round exists to kill: ctx.imagesCount = [P1: 0] must yield
    // images_count == 0, NOT 1. The old bare `?:` would have coerced this.
    def zeroImagesMeta = Meta.fromSamplesheetRow(noCountRow, 'image', 0, [channelsCount: [P1: 2], imagesCount: [P1: 0]])
    assert zeroImagesMeta.images_count == 0, 'a genuine images_count of 0 must NOT be coerced to 1'

    // ---- Fix round 1, item 2: fromCheckpointRow validates against the SCHEMA --
    //
    // lib/Checkpoint.groovy owns the column list per step; fromCheckpointRow now
    // reads it from there instead of trusting whatever the row happens to carry.
    // RULING R17 landed in Task 4.3: every Checkpoint.STEPS schema now carries an
    // 'id' column, so these calls can finally reach a successful return (see the
    // full-construction case near the end of this block) -- the schema-level "this
    // step doesn't declare id yet" throw is no longer reachable for a REAL step
    // name (only Checkpoint.UnknownStepException remains reachable that way, via
    // the 'not_a_real_step' case below). What replaces it as the load-bearing
    // migration check is row.containsKey('id') -- a REAL old checkpoint FILE has no
    // 'id' column in its header, so splitCsv(header:true) never puts an 'id' key in
    // the row Map at all, distinct from "the column exists but this row's value is
    // blank" (a separate, more mundane problem covered further down).

    // 'preprocessed' declares is_reference AND channels (Checkpoint.columns).
    // Missing is_reference is caught BEFORE channels is even inspected.
    def missingIsReference = false
    try { Meta.fromCheckpointRow([patient_id: 'P1', channels: 'DAPI'], 'preprocessed', [:]) }
    catch (IllegalArgumentException ignored) { missingIsReference = true }
    assert missingIsReference, "Meta.fromCheckpointRow must reject a 'preprocessed' row with no is_reference"

    // Satisfy is_reference -- the failure moves to the id gate (IllegalStateException),
    // because this row (an id-less-file shape) still carries no 'id' key at all. The
    // message must be something a user can act on: name the fix, not just the symptom.
    def afterIsReferenceFixed = null
    try { Meta.fromCheckpointRow([patient_id: 'P1', is_reference: 'true', channels: 'DAPI'], 'preprocessed', [:]) }
    catch (IllegalStateException e) { afterIsReferenceFixed = e.message }
    assert afterIsReferenceFixed?.contains('predates identity tracking') &&
           afterIsReferenceFixed?.contains('re-run the step'),
        'fixing is_reference on an id-less preprocessed row must move the failure on to the id gate, with an actionable message'

    // Missing channels specifically (is_reference present) on the same schema.
    def missingChannelsCol = false
    try { Meta.fromCheckpointRow([patient_id: 'P1', is_reference: 'true'], 'preprocessed', [:]) }
    catch (IllegalArgumentException ignored) { missingChannelsCol = true }
    assert missingChannelsCol, "Meta.fromCheckpointRow must reject a 'preprocessed' row with no channels"

    // 'postprocessed' declares NEITHER is_reference NOR channels (Checkpoint.columns).
    // A row missing both must NOT be rejected for those -- it must fall straight
    // through to the id gate, proving the schema-conditional checks are actually
    // schema-conditional and not just always-on.
    def postprocessedFailure = null
    try { Meta.fromCheckpointRow([patient_id: 'P1'], 'postprocessed', [:]) }
    catch (IllegalStateException e) { postprocessedFailure = e.message }
    assert postprocessedFailure?.contains('predates identity tracking'),
        "a 'postprocessed' row with no is_reference/channels must fail on the id gate, not on those fields"

    // The column EXISTS (the row Map has an 'id' key) but this one row's VALUE is
    // blank -- a different, more mundane problem (a malformed/hand-edited file),
    // correctly caught by requirePresentInRow's generic message instead of the
    // richer "predates identity tracking" one.
    def blankIdValue = false
    try {
        Meta.fromCheckpointRow(
            [patient_id: 'P1', id: '', is_reference: 'true', channels: 'DAPI'],
            'preprocessed', [channelsCount: [P1: 1], imagesCount: [P1: 1]])
    }
    catch (IllegalArgumentException ignored) { blankIdValue = true }
    assert blankIdValue, 'a row with a blank id VALUE (column present) must fail requirePresentInRow, not the id-gate message'

    // Fully satisfied: fromCheckpointRow can now actually return a complete meta --
    // unreachable before Task 4.3 added the id column to every schema.
    def ckCtx = [
        keepChannelsBySlide: [P1: [P1_slide: ['DAPI', 'CD3']]],
        imagesCount        : [P1: 1],
        channelsCount      : [P1: 2],
    ]
    def ckRow  = [patient_id: 'P1', id: 'P1_slide', is_reference: 'true', channels: 'DAPI|CD3']
    def ckMeta = Meta.fromCheckpointRow(ckRow, 'preprocessed', ckCtx)
    assert Meta.REQUIRED_KEYS.every { ckMeta.containsKey(it) }
    assert ckMeta.patient_id     == 'P1'
    assert ckMeta.id             == 'P1_slide'
    assert ckMeta.is_reference   == true
    assert ckMeta.channels       == ['DAPI', 'CD3']
    assert ckMeta.keep_channels  == ['DAPI', 'CD3']
    assert ckMeta.channels_count == 2
    assert ckMeta.images_count   == 1

    // An unknown step name is rejected via Checkpoint.columns, not silently accepted.
    def unknownStep = false
    try { Meta.fromCheckpointRow([patient_id: 'P1'], 'not_a_real_step', [:]) }
    catch (IllegalArgumentException ignored) { unknownStep = true }
    assert unknownStep, 'Meta.fromCheckpointRow must reject an unknown step name'

    // ------------------------------------------------------------------ //
    // CsvUtils.metaContextFromCheckpoint (Task 4.3) -- the checkpoint-side
    // twin of the samplesheet-side ctx assembly INPUT_CHECK does by hand
    // (countImagesPerPatient / resolveKeptChannelsPerSlide / countChannelsPerPatient).
    // ------------------------------------------------------------------ //
    def ckpTmp = File.createTempFile('meta_ctx_checkpoint', '.csv')
    ckpTmp.deleteOnExit()
    ckpTmp.text =
        'patient_id,id,registered_image,is_reference,channels\n' +
        'P1,P1_ref,/x/ref.tiff,true,DAPI|PANCK|SMA\n' +
        // The moving slide re-declares DAPI (a re-stain) -- claim-once must drop it,
        // exactly as resolveKeptChannelsPerSlide does for a samplesheet's rows.
        'P1,P1_mov,/x/mov.tiff,false,DAPI|CD3\n'
    def checkpointCtx = CsvUtils.metaContextFromCheckpoint(ckpTmp.path, 'registered')
    assert checkpointCtx.imagesCount == [P1: 2]
    assert checkpointCtx.channelsCount == [P1: 4]  // 3 (ref) + 1 (mov's CD3; DAPI already claimed)
    assert checkpointCtx.keepChannelsBySlide.P1.P1_ref == ['DAPI', 'PANCK', 'SMA']
    assert checkpointCtx.keepChannelsBySlide.P1.P1_mov == ['CD3']

    // A nonexistent file must degrade to the same empty shape every other CsvUtils
    // reader uses for "file not found" -- never throw, since a caller learns that
    // from Meta.fromCheckpointRow's own per-patient-count checks instead.
    def missingCtx = CsvUtils.metaContextFromCheckpoint('/no/such/checkpoint.csv', 'registered')
    assert missingCtx == [keepChannelsBySlide: [:], imagesCount: [:], channelsCount: [:]]

    // A schema with no channels/is_reference ('postprocessed') must not throw --
    // it degrades to empty keep-sets/zero counts rather than being stricter than
    // the Meta.fromCheckpointRow it feeds.
    def ppTmp = File.createTempFile('meta_ctx_postprocessed', '.csv')
    ppTmp.deleteOnExit()
    ppTmp.text = 'patient_id,id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid\n' +
        'P1,P1,/x/a.csv,/x/b.geojson,/x/c.csv,/x/d.tif,/x/e.tiff\n'
    def ppCtx = CsvUtils.metaContextFromCheckpoint(ppTmp.path, 'postprocessed')
    assert ppCtx.imagesCount == [P1: 1]
    assert ppCtx.channelsCount == [P1: 0]
    assert ppCtx.keepChannelsBySlide == [P1: [P1: []]]

    // Fail loudly, naming the missing key, rather than defaulting.
    def missingPatientId = false
    try { Meta.fromSamplesheetRow([image: 'x.tiff', channels: 'DAPI'], 'image', 0, [:]) }
    catch (IllegalArgumentException ignored) { missingPatientId = true }
    assert missingPatientId, 'Meta.fromSamplesheetRow must reject a row with no patient_id'

    def missingImageCol = false
    try { Meta.fromSamplesheetRow([patient_id: 'P1', channels: 'DAPI'], 'image', 0, [:]) }
    catch (IllegalArgumentException ignored) { missingImageCol = true }
    assert missingImageCol, 'Meta.fromSamplesheetRow must reject a row missing the image column'

    def blankStep = false
    try { Meta.fromCheckpointRow([patient_id: 'P1', id: 'x'], '  ', [:]) }
    catch (IllegalArgumentException ignored) { blankStep = true }
    assert blankStep, 'Meta.fromCheckpointRow must reject a blank step'

    // println, NOT log.info: nf-test's underlying `nextflow ... -quiet` run
    // suppresses log.info from stdout entirely (observed directly: a log.info
    // line here never appears in workflow.stdout under nf-test, even though the
    // exact same script printed it fine under a plain `nextflow run`). println
    // writes straight to stdout regardless of -quiet, so it is what
    // tests/lib_probe.nf.test's `workflow.stdout.any { ... }` assertion needs.
    println "LIB PROBE: all assertions passed"
}
