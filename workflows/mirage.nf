/*
================================================================================
    MIRAGE WSI Processing Pipeline — Main Workflow
================================================================================
*/

include { PREPROCESSING       } from '../subworkflows/local/preprocess'
include { REGISTRATION        } from '../subworkflows/local/registration'
include { POSTPROCESSING      } from '../subworkflows/local/postprocess'
include { ADD_CYCLE           } from '../subworkflows/local/add_cycle'
include { AGGREGATE_SIZE_LOGS         } from '../modules/local/aggregate_size_logs'
include { GENERATE_QC_REPORT          } from '../modules/local/generate_qc_report'
include { EXTRACT_MASK_SERIES         } from '../modules/local/extract_mask_series'


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

    /* -------------------- MODE: ADD_CYCLE -------------------- */
    if (params.mode == 'add_cycle') {
        ParamUtils.validateAddCycle(params.prior_outdir)
        ParamUtils.validateSegMethod(params.seg_method)  // reuse mask; still validate quant options
        ParamUtils.validateCompartmentQuant(params.quantify_compartments, params.expanded_quantification)
        ParamUtils.validateRegQc((params.reg_qc == null ? 1 : params.reg_qc) as int)
        ParamUtils.validateRegistrationPath(params)      // reg_qc=2 needs the classic registrar pickle

        if (!params.input) error "mode='add_cycle' requires --input (the new cycle samplesheet)"
        CsvUtils.validateInputCSV(params.input, ParamUtils.requiredColumnsForStep('preprocessing'))

        // The new-cycle samplesheet intentionally has NO reference row: the
        // registration reference is the frozen prior-run reference (external to
        // this CSV). Pass allow-no-reference=true so validation does not reject
        // the by-design zero-reference sheet. (Registration still uses the prior
        // reference — ADD_CYCLE forces the new cycle to is_reference=false.)
        CsvUtils.validateInputSemantics(params.input, 'preprocessing', true)

        // Fast-fail: every new-cycle patient must exist in the prior run's
        // postprocessed checkpoint, else its masks/base-table can't be sourced.
        def newPatients   = CsvUtils.countImagesPerPatient(params.input).keySet()
        def priorPostCsv  = "${params.prior_outdir}/csv/postprocessed.csv"
        def priorPatients = CsvUtils.countImagesPerPatient(priorPostCsv).keySet()
        def orphans = newPatients - priorPatients
        if (orphans) {
            error "mode='add_cycle': new-cycle patient(s) ${orphans} have no entry in ${priorPostCsv}. " +
                  "Each new-cycle patient_id must match a patient from the prior completed run."
        }

        if (params.dry_run) {
            log.info "DRY RUN (add_cycle): validations passed for --input=${params.input}, --prior_outdir=${params.prior_outdir}; mask extraction will run against ${params.prior_outdir}/csv/postprocessed.csv's pyramid column."
            return
        }

        // New-cycle raw images -> [meta, file] (reuse existing loader + counts).
        def new_counts    = CsvUtils.countImagesPerPatient(params.input)
        def new_ch_counts = CsvUtils.countChannelsPerPatient(params.input)
        def ch_new_input  = loadInputChannel(params.input, 'path_to_file', new_counts, new_ch_counts)

        // Prior reusable assets from the previous run's checkpoint CSVs.
        // registered.csv: patient_id,registered_image,is_reference,channels
        def ch_prior_ref = Channel
            .fromPath("${params.prior_outdir}/csv/registered.csv", checkIfExists: true)
            .splitCsv(header: true)
            .filter { row -> row.is_reference?.toLowerCase() == 'true' }
            .map { row ->
                def chans = (row.channels ?: '').split('\\|').collect { it.trim() }.findAll { it }
                [row.patient_id, chans, file(row.registered_image)]
            }

        // postprocessed.csv: patient_id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid
        def ch_prior_rows = Channel
            .fromPath("${params.prior_outdir}/csv/postprocessed.csv", checkIfExists: true)
            .splitCsv(header: true)
            .map { row -> [row.patient_id, file(row.merged_csv), file(row.cell_mask), file(row.pyramid)] }

        // Extract masks from the prior pyramid's Image:1 series (fast-fails in the
        // process if the prior run was not embed_masks+expanded+compartment).
        EXTRACT_MASK_SERIES(ch_prior_rows.map { pid, _merged_csv, _cell_mask, pyramid -> [[patient_id: pid, id: pid], pyramid] })
        def ch_masks = EXTRACT_MASK_SERIES.out.cell_mask.map { m, f -> [m.patient_id, f] }
            .join(EXTRACT_MASK_SERIES.out.nuclei_mask.map { m, f -> [m.patient_id, f] }, by: 0)

        def ch_prior_post = ch_prior_rows
            .map { pid, merged_csv, _cell_mask, pyramid -> [pid, merged_csv, pyramid] }
            .join(ch_masks, by: 0)
            .map { pid, merged_csv, pyramid, cell_mask, nuclei_mask ->
                [pid, merged_csv, cell_mask, nuclei_mask, pyramid] }

        // Join into a single per-patient asset tuple.
        def ch_prior_assets = ch_prior_ref
            .join(ch_prior_post, by: 0)
            .map { pid, ref_channels, ref_image, merged_csv, cell_mask, nuclei_mask, pyramid ->
                [pid, ref_channels, ref_image, merged_csv, cell_mask, nuclei_mask, pyramid]
            }

        ADD_CYCLE(ch_new_input, ch_prior_assets)
        return   // do NOT fall through to the standard start/stop flow
    }

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
        ParamUtils.validateRegQc((params.reg_qc == null ? 1 : params.reg_qc) as int)
        ParamUtils.validateRegistrationPath(params)      // reg_qc=2 needs the classic registrar pickle
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
        def ch_seg_eval_csv         = Channel.empty()
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
            ch_seg_eval_csv = ch_seg_eval_csv.mix(POSTPROCESSING.out.seg_eval_metrics)
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
            ch_seg_eval_csv.collect().ifEmpty([]),
            ch_collated_versions
        )
    }

    /* -------------------- TRACE AGGREGATION -------------------- */

    // Aggregate input size logs from all processes (only if tracing enabled)
    if (params.enable_trace) {
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
