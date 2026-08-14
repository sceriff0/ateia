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

    // A step's requiredColumns ARE the previous step's checkpoint columns — that is what
    // makes --start <step> able to read the checkpoint the previous step wrote. The two
    // tables state it independently, so assert they agree rather than trusting them to.
    // segmentation keeps the exact invariant (its requiredColumns == registered.csv's
    // full column list). postprocessing no longer can: segmented.csv gained four
    // columns beyond the base four, and postprocessing only requires the two it
    // dereferences unconditionally inside READ_SEGMENTED_CHECKPOINT (cell_mask,
    // nuclei_mask) -- so assert its exact (smaller) list, plus that every column it
    // names is still a real column of the checkpoint it reads (a subset check, not
    // an equality).
    assert ParamUtils.STEPS.find { it.name == 'registration'   }.requiredColumns == Checkpoint.columns(Layout.PREPROCESSED)
    assert ParamUtils.STEPS.find { it.name == 'segmentation'   }.requiredColumns == Checkpoint.columns(Layout.REGISTERED)
    assert ParamUtils.STEPS.find { it.name == 'postprocessing' }.requiredColumns ==
        ['patient_id', 'registered_image', 'is_reference', 'channels', 'cell_mask', 'nuclei_mask']
    assert ParamUtils.STEPS.find { it.name == 'postprocessing' }.requiredColumns
        .every { it in Checkpoint.columns(Layout.SEGMENTED) }

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
    // ParamUtils.compartmentMode / validateCompartmentQuant -- the
    // --quantify_compartments seam (mirrors --registration_method's: resolved
    // once, threaded down as an argument; see workflows/mirage.nf's
    // `compartment_mode` and tests/test_compartment_mode_routing.py).
    // ------------------------------------------------------------------ //

    // 1. Plain field mapping, all three flags on.
    def modeAllOn = ParamUtils.compartmentMode([
        quantify_compartments: true, quantify_statistics: ['Median','Mean','Sum'], embed_masks: true,
    ])
    assert modeAllOn == [compartments: true, statistics: ['Median','Mean','Sum'], expanded: true, embedMasks: true]

    // 2. All three off.
    def modeAllOff = ParamUtils.compartmentMode([
        quantify_compartments: false, quantify_statistics: ['Median'], embed_masks: false,
    ])
    assert modeAllOff == [compartments: false, statistics: ['Median'], expanded: false, embedMasks: false]

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
    assert WarpBackends.methods().toSorted() == ['tiled', 'valis']
    assert WarpBackends.container('valis') == 'cdgatenbee/valis-wsi:1.0.0'
    assert WarpBackends.container('tiled') == 'bolt3x/attend_image_analysis:tiled'
    assert WarpBackends.of('valis').stages == ['native', 'rigid', 'non_rigid', 'micro']
    assert WarpBackends.of('tiled').stages == ['native', 'rigid', 'refined']

    // The tiled backend must pass --method tiled; VALIS must not.
    assert WarpBackends.of('tiled').flags([:]).any { it.contains('--method tiled') }
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

    // println, NOT log.info: nf-test's underlying `nextflow ... -quiet` run
    // suppresses log.info from stdout entirely (observed directly: a log.info
    // line here never appears in workflow.stdout under nf-test, even though the
    // exact same script printed it fine under a plain `nextflow run`). println
    // writes straight to stdout regardless of -quiet, so it is what
    // tests/lib_probe.nf.test's `workflow.stdout.any { ... }` assertion needs.
    println "LIB PROBE: all assertions passed"
}
