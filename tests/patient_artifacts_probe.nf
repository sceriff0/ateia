/*
========================================================================================
    PatientArtifacts PROBE — the per-patient fan-in that must REFUSE to lose a patient
========================================================================================
    Same pattern, and for the same reason, as tests/patient_group_probe.nf: a
    `nextflow_function` test can only call a function declared in a `.nf` file, and
    PatientArtifacts is a lib/ class. So this is a pipeline script compiled WITH lib/
    on the classpath (`options "-lib $projectDir/lib"`), and the nf-test beside it
    asserts only how the run exited and what it printed.

    THE DEFECT THIS PINS
    --------------------
    `conf/modules.config`'s errorStrategy has an `'ignore'` branch: a task that fails
    the wrong way is DROPPED and the run keeps going. Every per-patient fan-in in this
    pipeline was a plain `Channel.join()` -- `failOnMismatch` and `failOnDuplicate`
    appeared NOWHERE in the repo -- so one ignored task on patient 7 removed patient 7
    from the join chain silently, and the run exited 0 having published a checkpoint
    CSV with 11 rows where 12 patients went in. Nothing anywhere said so; the short CSV
    is then read back by the next `--start` and the patient is simply gone.

    `--pa_case legacy` reproduces exactly that, with the hand-written chain
    subworkflows/local/postprocess.nf carried, so the silent drop is observable rather
    than asserted from memory.

    AND THE CHAIN WAS POSITIONAL
    ----------------------------
    That chain produced a 6-tuple destructured ~40 lines further down. `cell_csv` and
    `cell_geojson` are BOTH `Layout.publishedPath(..., 'geojson', ...)` of the same
    patient, so swapping the two `.join()` clauses swapped the two checkpoint columns
    and nothing noticed -- `Checkpoint.row` validates key PRESENCE, not which file
    landed under which key. `--pa_case transposed` swaps the two producers on purpose,
    so the test beside this file can watch the swap be observable (and therefore watch
    that the `named` assertions are the ones catching it).

    Cases:
      legacy      the hand-written positional chain, one patient short of a `pyramid`.
                  Exits 0 and emits ONE row for TWO patients. The defect, verbatim.
      missing     the same data through PatientArtifacts.bundle. Must ABORT, naming
                  the seam, the field and the patient.
      extra       a field carrying a patient the roster does not have. Must ABORT.
      duplicate   a patient emitted twice on one field. Must ABORT.
      named       the happy path: every field read BY NAME, keying conventions mixed.
      transposed  `named`, with cell_csv/cell_geojson bound to each other's producer.
      gate        a run-level gated field (`when: false`) falls back to a named sibling
                  rather than emptying the whole join.
      optional    a per-patient optional field: the patient with none still emits.
========================================================================================
*/

// ---------------------------------------------------------------------------
// The fixture: two patients, the postprocessed-checkpoint field set.
// tests/testdata/test_input_two_patients.csv is the run-level two-patient fixture;
// this probe needs the same cardinality without a pipeline, so it names the same ids.
// ---------------------------------------------------------------------------
def META = [
    P001: [patient_id: 'P001', id: 'P001_ref', is_reference: true, images_count: 2, channels_count: 2],
    P002: [patient_id: 'P002', id: 'P002_ref', is_reference: true, images_count: 2, channels_count: 2],
]

def metaCh = { List pids, String name ->
    Channel.fromList(pids.collect { pid -> [META[pid], file("${pid}_${name}")] })
}
def pidCh = { List pids, String name ->
    Channel.fromList(pids.collect { pid -> [pid, file("${pid}_${name}")] })
}

def BOTH = ['P001', 'P002']

workflow {

    // -------------------------------------------------------------------
    // legacy — the chain as postprocess.nf wrote it, with P002's pyramid dropped
    // -------------------------------------------------------------------
    if (params.pa_case == 'legacy') {
        metaCh(BOTH, 'cells_data.csv')
            .map { meta, f -> [meta.patient_id, f] }
            .join(metaCh(BOTH, 'cells.geojson').map { meta, f -> [meta.patient_id, f] })
            .join(metaCh(BOTH, 'merged.csv').map { meta, f -> [meta.patient_id, f] })
            .join(metaCh(BOTH, 'cell_mask.tif').map { meta, f -> [meta.patient_id, f] })
            // The dropped task: MERGE_AND_PYRAMID produced nothing for P002.
            .join(metaCh(['P001'], 'pyramid.ome.tif').map { meta, f -> [meta.patient_id, f] })
            .map { pid, cell_csv, cell_geojson, merged_csv, cell_mask, pyramid ->
                println "ROW: ${pid} cell_csv=${cell_csv.name} cell_geojson=${cell_geojson.name} " +
                        "merged_csv=${merged_csv.name} cell_mask=${cell_mask.name} pyramid=${pyramid.name}"
            }
        return
    }

    // -------------------------------------------------------------------
    // The strict versions. `metaFrom` names the field whose channel carries the
    // [meta, payload] shape -- it supplies the bundle's meta AND anchors the roster.
    // -------------------------------------------------------------------
    def emit = { ch ->
        ch.subscribe { b ->
            println "ROW: ${b.patient_id} meta_id=${b.meta.id} " +
                    "cell_csv=${b.cell_csv.name} cell_geojson=${b.cell_geojson.name} " +
                    "merged_csv=${b.merged_csv.name} cell_mask=${b.cell_mask.name} " +
                    "pyramid=${b.pyramid.name}"
        }
    }

    if (params.pa_case == 'missing') {
        emit(PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the postprocessed checkpoint row',
            metaFrom: 'cell_mask',
            fields  : [
                cell_mask   : metaCh(BOTH, 'cell_mask.tif'),
                cell_csv    : metaCh(BOTH, 'cells_data.csv'),
                cell_geojson: metaCh(BOTH, 'cells.geojson'),
                merged_csv  : metaCh(BOTH, 'merged.csv'),
                pyramid     : metaCh(['P001'], 'pyramid.ome.tif'),
            ],
        ))
    }
    else if (params.pa_case == 'extra') {
        emit(PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the postprocessed checkpoint row',
            metaFrom: 'cell_mask',
            fields  : [
                cell_mask   : metaCh(['P001'], 'cell_mask.tif'),
                cell_csv    : metaCh(['P001'], 'cells_data.csv'),
                cell_geojson: metaCh(['P001'], 'cells.geojson'),
                merged_csv  : metaCh(['P001'], 'merged.csv'),
                pyramid     : metaCh(BOTH, 'pyramid.ome.tif'),
            ],
        ))
    }
    else if (params.pa_case == 'duplicate') {
        emit(PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the postprocessed checkpoint row',
            metaFrom: 'cell_mask',
            fields  : [
                cell_mask   : metaCh(BOTH, 'cell_mask.tif'),
                cell_csv    : metaCh(BOTH, 'cells_data.csv'),
                cell_geojson: metaCh(BOTH, 'cells.geojson'),
                merged_csv  : metaCh(BOTH, 'merged.csv'),
                pyramid     : metaCh(['P001', 'P002', 'P002'], 'pyramid.ome.tif'),
            ],
        ))
    }
    else if (params.pa_case == 'named' || params.pa_case == 'transposed') {
        // cell_csv and cell_geojson are the transposable pair: both are
        // Layout.publishedPath(..., 'geojson', ...) of the same patient.
        def csv_producer     = metaCh(BOTH, 'cells_data.csv')
        def geojson_producer = metaCh(BOTH, 'cells.geojson')
        def transposed = params.pa_case == 'transposed'
        emit(PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the postprocessed checkpoint row',
            metaFrom: 'cell_mask',
            fields  : [
                cell_mask   : metaCh(BOTH, 'cell_mask.tif'),
                // Mixed keying on purpose: merged_csv arrives [meta, file] and
                // pyramid arrives [patient_id, file]. A caller must not have to know.
                cell_csv    : transposed ? geojson_producer : csv_producer,
                cell_geojson: transposed ? csv_producer     : geojson_producer,
                merged_csv  : metaCh(BOTH, 'merged.csv'),
                pyramid     : pidCh(BOTH, 'pyramid.ome.tif'),
            ],
        ))
    }
    else if (params.pa_case == 'gate') {
        PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the export tuple',
            metaFrom: 'merged_csv',
            fields  : [
                merged_csv      : metaCh(BOTH, 'merged.csv'),
                contours        : pidCh(BOTH, 'contours.json'),
                // --quantify_compartments is off: EXTRACT_NUCLEI_PROPERTIES never ran,
                // so the channel is empty. An empty channel joined plainly would empty
                // the WHOLE bundle; the gate makes the field fall back by name instead.
                nucleus_contours: [channel: Channel.empty(), when: false, orElseField: 'contours'],
            ],
        ).subscribe { b ->
            println "GATED: ${b.patient_id} contours=${b.contours.name} " +
                    "nucleus_contours=${b.nucleus_contours.name}"
        }
    }
    else if (params.pa_case == 'optional') {
        PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the SpatialData export',
            metaFrom: 'merged_csv',
            fields  : [
                merged_csv: metaCh(BOTH, 'merged.csv'),
                // Genuinely per-patient optional: a single-slide patient produces no
                // registration QC at all. Missing is a value, not a dropped patient.
                reg_qc    : [channel: pidCh(['P001'], 'seg_qc.json'), optional: true, orElse: []],
            ],
        ).subscribe { b ->
            println "OPTIONAL: ${b.patient_id} reg_qc=${b.reg_qc instanceof List && b.reg_qc.isEmpty() ? 'NONE' : b.reg_qc.name}"
        }
    }
    else {
        throw new IllegalArgumentException("unknown --pa_case '${params.pa_case}'")
    }
}
