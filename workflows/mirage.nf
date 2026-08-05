/*
================================================================================
    MIRAGE WSI Processing Pipeline — Main Workflow
================================================================================
*/

import groovy.json.JsonOutput

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
                // Map addition (+) returns a new map; clone() would only
                // shallow-copy and alias meta.channels across derived metas.
                def updated_meta = meta + [images_count: count]
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
                // Map addition (+) returns a new map; clone() would only
                // shallow-copy and alias meta.channels across derived metas.
                def updated_meta = meta + [channels_count: count]
                [updated_meta, f]
            }
    }

    // Fail-fast guard: `.combine(by: 0)` above is an INNER join keyed on
    // patient_id. If a row's patient_id does not exactly match a key in the
    // pre-computed counts map (e.g. stray whitespace CsvUtils.parseMetadata
    // failed to trim), the row is silently dropped with no warning or error —
    // that is the exact mechanism of the samplesheet-row-drop bug this guard
    // closes. Compare what actually reaches the channel against the
    // independently-computed per-patient image total and error loudly (not
    // log.warn) on any shortfall, naming patient_id so the cause is obvious.
    if (patient_counts) {
        def expected_count = patient_counts.values().sum() ?: 0
        ch.count().subscribe { emitted ->
            if (emitted < expected_count) {
                error "loadInputChannel(${csv_path}): ${expected_count - emitted} row(s) were silently dropped " +
                      "joining on patient_id (expected ${expected_count} row(s), got ${emitted}). This means a " +
                      "row's patient_id does not exactly match the value used to pre-compute per-patient counts " +
                      "(e.g. leading/trailing whitespace) so the inner-join combine(by: 0) discarded it."
            }
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
    ParamUtils.validateOutdir(params.outdir)

    /* -------------------- MODE: ADD_CYCLE -------------------- */
    if (params.mode == 'add_cycle') {
        ParamUtils.validateAddCycle(params.prior_outdir)
        ParamUtils.validateSegMethod(params.seg_method)  // reuse mask; still validate quant options
        ParamUtils.validateCompartmentQuant(params.quantify_compartments, params.expanded_quantification)
        ParamUtils.validateRegQc((params.reg_qc == null ? 2 : params.reg_qc) as int)
        ParamUtils.validateMicroReg(ParamUtils.microRegLevel(params))
        ParamUtils.validateAddCyclePhenotyping(params)  // add_cycle has no PHENOTYPE stage
        // add_cycle re-registers the new cycle via the classic VALIS_ADAPTER only; the
        // STARE 'tiled' backend is not wired into the incremental path — reject it loudly
        // rather than silently registering with VALIS.
        if (params.registration_method == 'tiled') {
            error "mode='add_cycle' does not support --registration_method tiled (STARE) yet; use valis."
        }

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

        /* -------------------- FINAL QC REPORT (add_cycle) -------------------- */
        // Mirrors the standard-path aggregation below (version collation + QC report +
        // optional size-log aggregation), scoped to what ADD_CYCLE actually emits.
        // ADD_CYCLE has no separate PREPROCESSING-QC / feature-distance / VALIS-summary /
        // POSTPROCESSING-QC / seg-eval emits of its own (it calls PREPROCESSING internally
        // but does not re-expose its QC pngs, and it has no POSTPROCESSING/CSE step at
        // all — masks are reused, not re-segmented) — those five report inputs are
        // deliberately left empty here rather than silently dropping the whole report.
        if (!params.skip_final_qc_report) {
            def ch_collated_versions = ADD_CYCLE.out.versions
                .unique()
                .collectFile(name: 'collated_versions.yml')

            def run_summary_map = [
                pipeline: [name: workflow.manifest.name, version: workflow.manifest.version],
                run: [
                    timestamp: new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss 'UTC'")
                        .format(new Date()),
                    mode: 'add_cycle',
                    start: params.start,
                    stop: 'add_cycle',
                ],
                params: [
                    registration_method: params.registration_method,
                    seg_method: params.seg_method,
                    quantify_compartments: params.quantify_compartments,
                    expanded_quantification: params.expanded_quantification,
                    pixel_size: params.pixel_size,
                    cse_pixel_size_um: params.cse_pixel_size_um,
                ],
                manifest: [
                    totals: [
                        patients: new_counts.size(),
                        images: (new_counts.values().sum() ?: 0),
                        channels: (new_ch_counts.values().sum() ?: 0),
                    ],
                    patients: new_counts.collectEntries { pid, imgs ->
                        [(pid): [images: imgs, channels: (new_ch_counts[pid] ?: 0)]]
                    },
                ],
            ]
            def ch_run_summary = Channel
                .of(JsonOutput.prettyPrint(JsonOutput.toJson(run_summary_map)))
                .collectFile(name: 'run_summary.json')

            GENERATE_QC_REPORT(
                Channel.empty().collect().ifEmpty([]),                             // preprocess_qc_pngs (not re-emitted by ADD_CYCLE)
                ADD_CYCLE.out.qc.map { meta, files -> files }.collect().ifEmpty([]),
                Channel.empty().collect().ifEmpty([]),                             // feature_distance_jsons (not re-emitted by ADD_CYCLE)
                Channel.empty().collect().ifEmpty([]),                             // valis_summary_csvs (not re-emitted by ADD_CYCLE)
                Channel.empty().collect().ifEmpty([]),                             // postprocess_qc_pngs (no POSTPROCESSING call in add_cycle)
                Channel.empty().collect().ifEmpty([]),                             // seg_eval_csvs (no CSE step in add_cycle)
                ch_collated_versions,
                ch_run_summary,
                Channel.empty().collect().ifEmpty([]),                             // distance_plots (not produced in add_cycle)
                ADD_CYCLE.out.seg_qc.map { meta, files -> files }.collect().ifEmpty([]),
            )
        }

        /* -------------------- TRACE AGGREGATION (add_cycle) -------------------- */
        if (params.enable_trace) {
            def ch_merged_sizes = ADD_CYCLE.out.size_logs.collectFile(name: 'raw_input_sizes.csv', sort: true)
            AGGREGATE_SIZE_LOGS(ch_merged_sizes)
        }

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
        ParamUtils.validateRegQc((params.reg_qc == null ? 2 : params.reg_qc) as int)
        ParamUtils.validateMicroReg(ParamUtils.microRegLevel(params))
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

        // Registration QC feeds the SpatialData export's `uns`/`obsm`. It only exists
        // when REGISTRATION actually ran in this session — with --start postprocessing
        // there is no REGISTRATION output to reference, so pass empty channels and let
        // the export write a store without registration QC rather than fail.
        def ch_reg_qc_for_post = params.start == 'postprocessing'
            ? Channel.empty()
            : REGISTRATION.out.seg_qc
        def ch_reg_residuals_for_post = params.start == 'postprocessing'
            ? Channel.empty()
            : REGISTRATION.out.seg_residuals

        POSTPROCESSING(ch_for_postprocessing, ch_reg_qc_for_post, ch_reg_residuals_for_post)
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
        def ch_distance_plots = Channel.empty()
        def ch_seg_qc_jsons   = Channel.empty()

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
            ch_distance_plots = ch_distance_plots
                .mix(REGISTRATION.out.distance_plots.map { meta, files -> files })
            ch_seg_qc_jsons = ch_seg_qc_jsons
                .mix(REGISTRATION.out.seg_qc.map { meta, files -> files })
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

        // Run-context summary for the report's overview card (pipeline, run,
        // params, sample manifest). Built from data already computed above
        // (patient_counts/channel_counts) rather than re-parsing the CSV.
        def manifest_patients = patient_counts.collectEntries { pid, imgs ->
            [(pid): [images: imgs, channels: (channel_counts[pid] ?: 0)]]
        }
        def run_summary_map = [
            pipeline: [name: workflow.manifest.name, version: workflow.manifest.version],
            run: [
                timestamp: new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss 'UTC'")
                    .format(new Date()),
                mode: (params.mode ?: 'standard'),
                start: params.start,
                stop: effective_stop,
            ],
            params: [
                registration_method: params.registration_method,
                seg_method: params.seg_method,
                quantify_compartments: params.quantify_compartments,
                expanded_quantification: params.expanded_quantification,
                pixel_size: params.pixel_size,
                cse_pixel_size_um: params.cse_pixel_size_um,
            ],
            manifest: [
                totals: [
                    patients: patient_counts.size(),
                    images: (patient_counts.values().sum() ?: 0),
                    channels: (channel_counts.values().sum() ?: 0),
                ],
                patients: manifest_patients,
            ],
        ]
        def ch_run_summary = Channel
            .of(JsonOutput.prettyPrint(JsonOutput.toJson(run_summary_map)))
            .collectFile(name: 'run_summary.json')

        GENERATE_QC_REPORT(
            ch_preprocess_qc_pngs.collect().ifEmpty([]),
            ch_registration_qc_pngs.collect().ifEmpty([]),
            ch_feature_dist_jsons.collect().ifEmpty([]),
            ch_valis_summary_csvs.collect().ifEmpty([]),
            ch_postprocess_qc_pngs.collect().ifEmpty([]),
            ch_seg_eval_csv.collect().ifEmpty([]),
            ch_collated_versions,
            ch_run_summary,
            ch_distance_plots.collect().ifEmpty([]),
            ch_seg_qc_jsons.collect().ifEmpty([]),
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
