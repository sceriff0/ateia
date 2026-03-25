/*
================================================================================
    MIRAGE WSI Processing Pipeline — Main Workflow
================================================================================
*/

include { PREPROCESSING       } from '../subworkflows/local/preprocess'
include { REGISTRATION        } from '../subworkflows/local/registration'
include { POSTPROCESSING      } from '../subworkflows/local/postprocess'
include { AGGREGATE_SIZE_LOGS         } from '../modules/local/aggregate_size_logs'
include { GENERATE_QC_REPORT          } from '../modules/local/generate_qc_report'

import static CsvUtils.*
import static ParamUtils.*

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

    validateStep(params.step)

    if (params.step in ['preprocessing', 'registration']) {
        validateRegistrationMethod(params.registration_method)
    }

    if (params.step in ['preprocessing', 'registration', 'postprocessing']) {
        if (!params.input) {
            error "Please provide --input for step '${params.step}'"
        }
        validateInputCSV(
            params.input,
            requiredColumnsForStep(params.step)
        )
    }

    if (params.dry_run) {
        log.info "DRY RUN: all validations passed"
        return
    }

    /* -------------------- PREPROCESSING -------------------- */

    // Pre-count images and channels per patient for streaming groupTuple operations
    // (called after null-guard above ensures params.input is valid)
    def patient_counts = params.input ? CsvUtils.countImagesPerPatient(params.input) : [:]
    def channel_counts = params.input ? CsvUtils.countChannelsPerPatient(params.input) : [:]

    if (params.step == 'preprocessing') {
        def ch_input = loadInputChannel(params.input, 'path_to_file', patient_counts, channel_counts)
        PREPROCESSING(ch_input)
    }

    /* -------------------- REGISTRATION -------------------- */

    if (params.step in ['preprocessing','registration']) {

        // When starting from registration, params.input is a string path to CSV
        // When continuing from preprocessing, use direct channel (streaming, no wait)
        def ch_for_registration = params.step == 'registration'
            ? loadInputChannel(params.input, 'preprocessed_image', patient_counts, channel_counts)
            : PREPROCESSING.out.preprocessed  // Direct channel - enables patient-level parallelism!

        REGISTRATION(ch_for_registration)
    }

    /* -------------------- POSTPROCESSING -------------------- */

    if (params.step in ['preprocessing','registration','postprocessing']) {

        // When starting from postprocessing, params.input is a string path to CSV
        // When continuing from registration, use direct channel (streaming, no wait)
        def ch_for_postprocessing = params.step == 'postprocessing'
            ? loadInputChannel(params.input, 'registered_image', patient_counts, channel_counts)
            : REGISTRATION.out.registered  // Direct channel - enables patient-level parallelism!

        POSTPROCESSING(ch_for_postprocessing)
    }

    /* -------------------- FINAL QC REPORT -------------------- */

    // Aggregate QC outputs from all steps into a single HTML report
    if (!params.skip_final_qc_report) {
        // Collect QC outputs from each step (empty channels for steps not run)
        def ch_preprocess_qc_pngs  = Channel.empty()
        def ch_registration_qc_pngs = Channel.empty()
        def ch_feature_dist_jsons   = Channel.empty()
        def ch_postprocess_qc_pngs  = Channel.empty()
        def ch_versions             = Channel.empty()

        if (params.step == 'preprocessing') {
            ch_preprocess_qc_pngs = ch_preprocess_qc_pngs.mix(PREPROCESSING.out.preprocess_qc)
            ch_versions = ch_versions.mix(PREPROCESSING.out.versions)
        }
        if (params.step in ['preprocessing', 'registration']) {
            ch_registration_qc_pngs = ch_registration_qc_pngs
                .mix(REGISTRATION.out.qc.map { meta, files -> files })
            ch_feature_dist_jsons = ch_feature_dist_jsons
                .mix(REGISTRATION.out.error_metrics.map { meta, files -> files })
            ch_versions = ch_versions.mix(REGISTRATION.out.versions)
        }
        if (params.step in ['preprocessing', 'registration', 'postprocessing']) {
            ch_postprocess_qc_pngs = ch_postprocess_qc_pngs.mix(POSTPROCESSING.out.postprocess_qc)
            ch_versions = ch_versions.mix(POSTPROCESSING.out.versions)
        }

        // Collate versions into a single file
        ch_collated_versions = ch_versions
            .unique()
            .collectFile(name: 'collated_versions.yml')

        GENERATE_QC_REPORT(
            ch_preprocess_qc_pngs.collect().ifEmpty([]),
            ch_registration_qc_pngs.collect().ifEmpty([]),
            ch_feature_dist_jsons.collect().ifEmpty([]),
            Channel.empty().collect().ifEmpty([]),       // valis_summary (collected from registration adapter if available)
            ch_postprocess_qc_pngs.collect().ifEmpty([]),
            ch_collated_versions
        )
    }

    /* -------------------- TRACE AGGREGATION -------------------- */

    // Aggregate input size logs from all processes (only if tracing enabled)
    if (params.enable_trace) {
        def ch_all_sizes = Channel.empty()

        if (params.step == 'preprocessing') {
            ch_all_sizes = ch_all_sizes.mix(PREPROCESSING.out.size_logs)
        }
        if (params.step in ['preprocessing', 'registration']) {
            ch_all_sizes = ch_all_sizes.mix(REGISTRATION.out.size_logs)
        }
        if (params.step in ['preprocessing', 'registration', 'postprocessing']) {
            ch_all_sizes = ch_all_sizes.mix(POSTPROCESSING.out.size_logs)
        }

        AGGREGATE_SIZE_LOGS(ch_all_sizes.collect())
    }
}
