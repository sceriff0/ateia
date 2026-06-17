/*
================================================================================
    MIRAGE WSI Processing Pipeline — Main Workflow
================================================================================
*/

include { PREPROCESSING       } from '../subworkflows/local/preprocess'
include { REGISTRATION        } from '../subworkflows/local/registration'
include { POSTPROCESSING      } from '../subworkflows/local/postprocess'
include { AGGREGATE_SIZE_LOGS         } from '../benchmarks/modules/aggregate_size_logs'
include { GENERATE_QC_REPORT          } from '../modules/local/generate_qc_report'


/*
================================================================================
    HELPER FUNCTIONS
================================================================================
*/

def loadInputChannel(csv_path, image_column, patient_counts = null, channel_counts = null) {
    def ch = Channel
        .fromPath(csv_path, checkIfExists: true)
        .splitCsv(header: true)
        .map { row ->
            def meta = CsvUtils.parseMetadata(row, "CSV ${csv_path}")
            // Per-image unique id (patient_id + source-image stem). Drives output
            // file naming so a patient's multiple images do not produce identically
            // named files that collide when collected downstream (QC, registration).
            def stem = file(row[image_column]).simpleName
            meta.id = stem.startsWith(meta.patient_id) ? stem : "${meta.patient_id}_${stem}"
            return tuple(meta, file(row[image_column]))
        }

    // If patient counts provided, add images_count to meta for streaming groupTuple
    if (patient_counts) {
        def counts_ch = Channel.fromList(patient_counts.collect { k, v -> [k, v] })
        ch = ch
            .map { meta, f -> [meta.patient_id, meta, f] }
            .combine(counts_ch, by: 0)
            .map { patient_id, meta, f, count ->
                def updated_meta = meta.clone()
                updated_meta.images_count = count
                [updated_meta, f]
            }
    }

    // If channel counts provided, add channels_count to meta for streaming groupTuple in postprocessing
    if (channel_counts) {
        def ch_counts = Channel.fromList(channel_counts.collect { k, v -> [k, v] })
        ch = ch
            .map { meta, f -> [meta.patient_id, meta, f] }
            .combine(ch_counts, by: 0)
            .map { patient_id, meta, f, count ->
                def updated_meta = meta.clone()
                updated_meta.channels_count = count
                [updated_meta, f]
            }
    }
    return ch
}

/*
================================================================================
    WORKFLOW
================================================================================
*/

workflow MIRAGE {

    /* -------------------- PARAMETER VALIDATION -------------------- */

    ParamUtils.validateStart(params.start)

    // Validate and resolve --stop: default to last step if not provided
    if (params.stop) {
        ParamUtils.validateStop(params.stop, params.start)
    }
    def effective_stop = params.stop ?: ParamUtils.STEP_ORDER.last()

    // Per-step gate: ParamUtils.shouldRun(step, start, stop). Called inline at
    // each site because the strict Nextflow parser does not support invoking a
    // closure-typed local variable as a function.

    if (ParamUtils.shouldRun('registration', params.start, effective_stop)) {
        ParamUtils.validateRegistrationMethod(params.registration_method)
    }

    if (ParamUtils.shouldRun('postprocessing', params.start, effective_stop)) {
        ParamUtils.validateSegMethod(params.seg_method)
        ParamUtils.validateCompartmentQuant(params.quantify_compartments, params.expanded_quantification)
    }

    if (!params.input) {
        error "Please provide --input for start '${params.start}'"
    }
    CsvUtils.validateInputCSV(
        params.input,
        ParamUtils.requiredColumnsForStep(params.start)
    )

    // Fail-fast semantic validation (per-row format + per-patient reference
    // counts + file existence). Runs here so it is also exercised by --dry_run.
    CsvUtils.validateInputSemantics(
        params.input,
        params.start,
        params.allow_auto_reference
    )

    if (params.dry_run) {
        log.info "DRY RUN: all validations passed (start=${params.start}, stop=${effective_stop})"
        return
    }

    /* -------------------- PREPROCESSING -------------------- */

    // Pre-count images and channels per patient for streaming groupTuple operations
    // (called after null-guard above ensures params.input is valid)
    def patient_counts = params.input ? CsvUtils.countImagesPerPatient(params.input) : [:]
    def channel_counts = params.input ? CsvUtils.countChannelsPerPatient(params.input) : [:]

    if (ParamUtils.shouldRun('preprocessing', params.start, effective_stop)) {
        def ch_input = loadInputChannel(params.input, 'path_to_file', patient_counts, channel_counts)
        PREPROCESSING(ch_input)
    }

    /* -------------------- REGISTRATION -------------------- */

    if (ParamUtils.shouldRun('registration', params.start, effective_stop)) {

        // When starting from registration, params.input is a string path to CSV
        // When continuing from preprocessing, use direct channel (streaming, no wait)
        def ch_for_registration = params.start == 'registration'
            ? loadInputChannel(params.input, 'preprocessed_image', patient_counts, channel_counts)
            : PREPROCESSING.out.preprocessed  // Direct channel - enables patient-level parallelism!

        REGISTRATION(ch_for_registration)
    }

    /* -------------------- POSTPROCESSING -------------------- */

    if (ParamUtils.shouldRun('postprocessing', params.start, effective_stop)) {

        // When starting from postprocessing, params.input is a string path to CSV
        // When continuing from registration, use direct channel (streaming, no wait)
        def ch_for_postprocessing = params.start == 'postprocessing'
            ? loadInputChannel(params.input, 'registered_image', patient_counts, channel_counts)
            : REGISTRATION.out.registered  // Direct channel - enables patient-level parallelism!

        POSTPROCESSING(ch_for_postprocessing)
    }

    /* -------------------- FINAL QC REPORT -------------------- */

    // Aggregate QC outputs from all steps into a single HTML report
    if (!params.skip_final_qc_report) {
        // Collect QC outputs from each step (empty channels for steps not run)
        def ch_preprocess_qc_pngs   = Channel.empty()
        def ch_registration_qc_pngs = Channel.empty()
        def ch_feature_dist_jsons   = Channel.empty()
        def ch_valis_summary_csvs   = Channel.empty()
        def ch_postprocess_qc_pngs  = Channel.empty()
        def ch_versions             = Channel.empty()

        if (ParamUtils.shouldRun('preprocessing', params.start, effective_stop)) {
            ch_preprocess_qc_pngs = ch_preprocess_qc_pngs.mix(PREPROCESSING.out.preprocess_qc)
            ch_versions = ch_versions.mix(PREPROCESSING.out.versions)
        }
        if (ParamUtils.shouldRun('registration', params.start, effective_stop)) {
            ch_registration_qc_pngs = ch_registration_qc_pngs
                .mix(REGISTRATION.out.qc.map { meta, files -> files })
            ch_feature_dist_jsons = ch_feature_dist_jsons
                .mix(REGISTRATION.out.error_metrics.map { meta, files -> files })
            ch_valis_summary_csvs = ch_valis_summary_csvs
                .mix(REGISTRATION.out.valis_summary)
            ch_versions = ch_versions.mix(REGISTRATION.out.versions)
        }
        if (ParamUtils.shouldRun('postprocessing', params.start, effective_stop)) {
            ch_postprocess_qc_pngs = ch_postprocess_qc_pngs.mix(POSTPROCESSING.out.postprocess_qc)
            ch_versions = ch_versions.mix(POSTPROCESSING.out.versions)
        }

        // Collate versions into a single file
        def ch_collated_versions = ch_versions
            .unique()
            .collectFile(name: 'collated_versions.yml')

        GENERATE_QC_REPORT(
            ch_preprocess_qc_pngs.collect().ifEmpty([]),
            ch_registration_qc_pngs.collect().ifEmpty([]),
            ch_feature_dist_jsons.collect().ifEmpty([]),
            ch_valis_summary_csvs.collect().ifEmpty([]),
            ch_postprocess_qc_pngs.collect().ifEmpty([]),
            ch_collated_versions
        )
    }

    /* -------------------- TRACE AGGREGATION -------------------- */

    // Aggregate input size logs from all processes (only if tracing enabled)
    if (params.enable_size_logs) {
        def ch_all_sizes = Channel.empty()

        if (ParamUtils.shouldRun('preprocessing', params.start, effective_stop)) {
            ch_all_sizes = ch_all_sizes.mix(PREPROCESSING.out.size_logs)
        }
        if (ParamUtils.shouldRun('registration', params.start, effective_stop)) {
            ch_all_sizes = ch_all_sizes.mix(REGISTRATION.out.size_logs)
        }
        if (ParamUtils.shouldRun('postprocessing', params.start, effective_stop)) {
            ch_all_sizes = ch_all_sizes.mix(POSTPROCESSING.out.size_logs)
        }

        // Merge by content (not by staging many same-named files) so AGGREGATE
        // receives a single file and cannot hit a work-dir name collision —
        // several processes emit identically-named *.size.csv logs.
        def ch_merged_sizes = ch_all_sizes.collectFile(name: 'raw_input_sizes.csv', sort: true)
        AGGREGATE_SIZE_LOGS(ch_merged_sizes)
    }
}
