/*
================================================================================
    MIRAGE WSI Processing Pipeline — Main Workflow
================================================================================
    This file has ONE job: validate the launch parameters and route the run to
    the right subworkflow. Anything that shapes a channel, assembles a
    subworkflow's inputs, or aggregates its outputs belongs to that subworkflow:

      samplesheet -> sample channel        subworkflows/local/input_check.nf
      prior-run asset reconstruction       subworkflows/local/add_cycle.nf
      QC report + size-log aggregation     subworkflows/local/final_qc.nf
================================================================================
*/

include { validateParameters  } from 'plugin/nf-schema'

include { INPUT_CHECK         } from '../subworkflows/local/input_check'
include { PREPROCESSING       } from '../subworkflows/local/preprocess'
include { REGISTRATION        } from '../subworkflows/local/registration'
include { SEGMENTATION; READ_SEGMENTED_CHECKPOINT } from '../subworkflows/local/segmentation'
include { POSTPROCESSING      } from '../subworkflows/local/postprocess'
include { ADD_CYCLE           } from '../subworkflows/local/add_cycle'
include { FINAL_QC            } from '../subworkflows/local/final_qc'


workflow MIRAGE {

    /* -------------------- PARAMETER VALIDATION -------------------- */

    // Types, enums and ranges come from nextflow_schema.json (nf-schema). This
    // covers every parameter in one place, including the ones the hand-rolled
    // Groovy never checked, and it rejects a -params-file that delivers a
    // boolean as the STRING "false" instead of silently treating it as true.
    // Cross-parameter and filesystem rules that no JSON Schema can express
    // (--stop ordering, samplesheet semantics, add_cycle prerequisites) stay
    // below in Groovy.
    validateParameters()

    ParamUtils.validateOutdir(params.outdir)

    /* -------------------- MODE: ADD_CYCLE -------------------- */
    if (params.mode == 'add_cycle') {
        ParamUtils.validateAddCycle(params.outdir, params.prior_outdir)
        ParamUtils.validateCompartmentQuant(params.quantify_compartments, params.expanded_quantification)
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
        CsvUtils.validateInputSemantics(params.input, 'preprocessing', true, params.nuclear_markers)

        // Fast-fail: every new-cycle patient must exist in the prior run's
        // postprocessed checkpoint, else its masks/base-table can't be sourced.
        def newPatients   = CsvUtils.countImagesPerPatient(params.input).keySet()
        def priorPostCsv  = Layout.checkpointCsv(params.prior_outdir, Layout.POSTPROCESSED)
        def priorPatients = CsvUtils.countImagesPerPatient(priorPostCsv).keySet()
        def orphans = newPatients - priorPatients
        if (orphans) {
            error "mode='add_cycle': new-cycle patient(s) ${orphans} have no entry in ${priorPostCsv}. " +
                  "Each new-cycle patient_id must match a patient from the prior completed run."
        }

        if (params.dry_run) {
            log.info "DRY RUN (add_cycle): validations passed for --input=${params.input}, --prior_outdir=${params.prior_outdir}; mask extraction will run against ${priorPostCsv}'s pyramid column."
            return
        }

        // autoReference = false: add_cycle registers against the PRIOR run's reference,
        // which is never a row in this sheet, so no row here keeps its nuclear channel
        // and the new cycle's markers are the declared channels minus the nuclear one.
        INPUT_CHECK(params.input, 'path_to_file', false)

        // ADD_CYCLE rebuilds the prior run's reusable assets itself, from
        // --prior_outdir's checkpoint CSVs.
        ADD_CYCLE(INPUT_CHECK.out.samples)

        // ADD_CYCLE has no preprocess_qc / registration_tre / postprocess_qc / seg_residuals
        // of its own (it calls PREPROCESSING internally without re-exposing its QC pngs, has
        // no POSTPROCESSING step at all — masks are reused, not re-segmented — and calls
        // SEG_QC without capturing `.out.per_cell`). Those kinds are simply not contributed;
        // FINAL_QC defaults them to empty.
        FINAL_QC(
            Channel.empty()
                .mix(ADD_CYCLE.out.qc.map        { _meta, files -> ['registration_qc', files] })
                .mix(ADD_CYCLE.out.seg_qc.map    { _meta, files -> ['seg_qc', files] })
                .mix(ADD_CYCLE.out.versions.map  { f -> ['versions', f] })
                .mix(ADD_CYCLE.out.size_logs.map { f -> ['size_log', f] }),
            INPUT_CHECK.out.counts.map { counts -> counts + [stop: 'add_cycle'] }
        )

        return   // do NOT fall through to the standard start/stop flow
    }

    /* -------------------- STEP GATE -------------------- */

    // Validate and resolve --stop: default to last step if not provided
    if (params.stop) {
        ParamUtils.validateStop(params.stop, params.start)
    }
    def effective_stop = params.stop ?: ParamUtils.STEP_ORDER.last()

    // Which steps this run covers. Computed once: the gate is a pure function of
    // (start, stop), so re-asking it at every site only creates opportunities for
    // the three arguments to disagree. (A closure would read better still, but the
    // strict Nextflow parser cannot invoke a closure-typed local as a function.)
    boolean run_preprocessing  = ParamUtils.shouldRun('preprocessing', params.start, effective_stop)
    boolean run_registration   = ParamUtils.shouldRun('registration', params.start, effective_stop)
    boolean run_segmentation   = ParamUtils.shouldRun('segmentation', params.start, effective_stop)
    boolean run_postprocessing = ParamUtils.shouldRun('postprocessing', params.start, effective_stop)

    if (run_postprocessing) {
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
        params.allow_auto_reference,
        params.nuclear_markers
    )

    if (params.dry_run) {
        log.info "DRY RUN: all validations passed (start=${params.start}, stop=${effective_stop})"
        return
    }

    /* -------------------- INPUT -------------------- */

    // The samplesheet is read exactly once, at the entry step: which column holds
    // the image to carry forward is fixed by --start, because each step's
    // checkpoint CSV names the file that step produced. Later steps consume the
    // previous step's channel directly (streaming, patient-level parallelism), so
    // only the entry step ever reads the sheet.
    def entry_column = ParamUtils.entryColumnForStep(params.start)

    // autoReference = params.allow_auto_reference: when a patient marks no reference,
    // registration.nf promotes its first image, which keeps that slide's nuclear
    // channel in the split output and therefore in the count.
    INPUT_CHECK(params.input, entry_column, params.allow_auto_reference)

    /* -------------------- PREPROCESSING -------------------- */

    if (run_preprocessing) {
        PREPROCESSING(INPUT_CHECK.out.samples)
    }

    /* -------------------- REGISTRATION -------------------- */

    if (run_registration) {
        REGISTRATION(
            ParamUtils.isEntryPoint(params, 'registration')
                ? INPUT_CHECK.out.samples
                : PREPROCESSING.out.preprocessed  // Direct channel - enables patient-level parallelism!
        )
    }

    /* -------------------- SEGMENTATION -------------------- */

    if (run_segmentation) {
        SEGMENTATION(
            ParamUtils.isEntryPoint(params, 'segmentation')
                ? INPUT_CHECK.out.samples
                : REGISTRATION.out.registered  // Direct channel - enables patient-level parallelism!
        )
    }

    /* -------------------- POSTPROCESSING -------------------- */

    if (run_postprocessing) {

        // Registration QC feeds the SpatialData export's `uns`/`obsm`. It only exists
        // when REGISTRATION actually ran IN THIS SESSION. Gated on run_registration
        // itself (not on isEntryPoint('postprocessing')): with the segmentation step
        // now sitting between registration and postprocessing, "postprocessing is not
        // the entry point" no longer implies registration ran this session — entry
        // could be 'segmentation' instead, in which case REGISTRATION was never
        // invoked and referencing REGISTRATION.out would fail outright.
        def ch_reg_qc_for_post        = run_registration ? REGISTRATION.out.seg_qc        : Channel.empty()
        def ch_reg_residuals_for_post = run_registration ? REGISTRATION.out.seg_residuals : Channel.empty()

        def ch_for_postprocessing
        def ch_cell_mask_for_post
        def ch_nuclei_mask_for_post
        def ch_contours_for_post
        def ch_nucleus_contours_for_post
        def ch_morphology_for_post

        if (ParamUtils.isEntryPoint(params, 'postprocessing')) {
            // The ONE place INPUT_CHECK's [meta, one_file] shape is not enough:
            // postprocessing's entry checkpoint (segmented.csv) carries four more
            // columns beyond a single image path. READ_SEGMENTED_CHECKPOINT
            // (subworkflows/local/segmentation.nf) is the dedicated reader —
            // mirrors add_cycle.nf's own splitCsv checkpoint readers.
            READ_SEGMENTED_CHECKPOINT(params.input)
            ch_for_postprocessing       = READ_SEGMENTED_CHECKPOINT.out.samples
            ch_cell_mask_for_post       = READ_SEGMENTED_CHECKPOINT.out.cell_mask
            ch_nuclei_mask_for_post     = READ_SEGMENTED_CHECKPOINT.out.nuclei_mask
            ch_contours_for_post        = READ_SEGMENTED_CHECKPOINT.out.contours
            ch_nucleus_contours_for_post = READ_SEGMENTED_CHECKPOINT.out.nucleus_contours
            ch_morphology_for_post      = READ_SEGMENTED_CHECKPOINT.out.morphology
        } else {
            // postprocessing is not the entry, so segmentation ran this session
            // (the only other way to reach postprocessing in the linear gate).
            ch_for_postprocessing = ParamUtils.isEntryPoint(params, 'segmentation')
                ? INPUT_CHECK.out.samples
                : REGISTRATION.out.registered  // Direct channel - enables patient-level parallelism!
            ch_cell_mask_for_post        = SEGMENTATION.out.cell_mask
            ch_nuclei_mask_for_post      = SEGMENTATION.out.nuclei_mask
            ch_contours_for_post         = SEGMENTATION.out.contours
            ch_nucleus_contours_for_post = SEGMENTATION.out.nucleus_contours
            ch_morphology_for_post       = SEGMENTATION.out.morphology
        }

        POSTPROCESSING(
            ch_for_postprocessing,
            ch_cell_mask_for_post,
            ch_nuclei_mask_for_post,
            ch_contours_for_post,
            ch_nucleus_contours_for_post,
            ch_morphology_for_post,
            ch_reg_qc_for_post,
            ch_reg_residuals_for_post
        )
    }

    /* -------------------- FINAL QC + TRACE -------------------- */

    // One tagged stream of everything the run produced for reporting. FINAL_QC
    // owns both end-of-run aggregations and both of their param gates
    // (--skip_final_qc_report, --enable_trace), so there is nothing to branch on
    // here — steps that did not run simply contribute nothing.
    def ch_qc_artifacts = Channel.empty()

    if (run_preprocessing) {
        ch_qc_artifacts = ch_qc_artifacts
            .mix(PREPROCESSING.out.preprocess_qc.map { f -> ['preprocess_qc', f] })
            .mix(PREPROCESSING.out.versions.map      { f -> ['versions', f] })
            .mix(PREPROCESSING.out.size_logs.map     { f -> ['size_log', f] })
    }
    if (run_registration) {
        ch_qc_artifacts = ch_qc_artifacts
            .mix(REGISTRATION.out.qc.map               { _meta, files -> ['registration_qc', files] })
            .mix(REGISTRATION.out.seg_qc.map           { _meta, files -> ['seg_qc', files] })
            .mix(REGISTRATION.out.seg_residuals.map    { _meta, files -> ['seg_residuals', files] })
            .mix(REGISTRATION.out.registration_tre.map { f -> ['registration_tre', f] })
            .mix(REGISTRATION.out.versions.map         { f -> ['versions', f] })
            .mix(REGISTRATION.out.size_logs.map        { f -> ['size_log', f] })
    }
    if (run_segmentation) {
        // No dedicated QC-image kind: ParamUtils.STEPS' 'segmentation' entry declares
        // qcKinds: [] on purpose (see its comment) — SEGMENT and the two property
        // extractors only ever contribute the two UNIVERSAL_QC_KINDS.
        ch_qc_artifacts = ch_qc_artifacts
            .mix(SEGMENTATION.out.versions.map  { f -> ['versions', f] })
            .mix(SEGMENTATION.out.size_logs.map { f -> ['size_log', f] })
    }
    if (run_postprocessing) {
        ch_qc_artifacts = ch_qc_artifacts
            .mix(POSTPROCESSING.out.postprocess_qc.map { f -> ['postprocess_qc', f] })
            .mix(POSTPROCESSING.out.versions.map       { f -> ['versions', f] })
            .mix(POSTPROCESSING.out.size_logs.map      { f -> ['size_log', f] })
    }

    FINAL_QC(
        ch_qc_artifacts,
        INPUT_CHECK.out.counts.map { counts -> counts + [stop: effective_stop] }
    )
}
