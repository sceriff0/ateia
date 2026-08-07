/*
 * ParamUtils - validation helpers for the pipeline's step-routing parameters.
 *
 * Scope note: single-parameter checks (is this a valid step name? a valid
 * segmentation backend? a reg_qc level in range?) are NOT here. They are stated
 * once in nextflow_schema.json and enforced by nf-schema's validateParameters()
 * at the top of workflows/mirage.nf. What remains below is what no JSON Schema
 * can express: cross-parameter rules (--stop must not precede --start,
 * --expanded_quantification implies --quantify_compartments), mode-conditional
 * rules (add_cycle), filesystem prerequisites, and plain control-flow helpers.
 */
class ParamUtils {

    /*
     * The single table answering "what is a step?". Everything else that used
     * to restate the step vocabulary — STEP_ORDER, requiredColumnsForStep,
     * mirage.nf's entry_column map, final_qc.nf's KNOWN_ARTIFACT_KINDS, and
     * nextflow_schema.json's start/stop enums — is either derived from this or
     * checked against it (the schema can't be generated at build time, so
     * tests/test_step_vocabulary_consistency.py asserts it agrees instead).
     *
     *   name            - the step's canonical identifier; also the value
     *                      --start/--stop accept.
     *   requiredColumns - samplesheet columns CsvUtils must find when this step
     *                      is the run's entry point (its own input, not a
     *                      downstream checkpoint column).
     *   entryColumn     - the checkpoint-CSV column INPUT_CHECK reads when this
     *                      step is the run's entry point (mirage.nf reads the
     *                      sheet exactly once, at --start).
     *   qcKinds         - the artifact-stream tags this step alone contributes
     *                      to FINAL_QC (see final_qc.nf). 'versions' and
     *                      'size_log' are cross-cutting — every step emits them
     *                      — so they are not listed per-step; they are added
     *                      once in UNIVERSAL_QC_KINDS below.
     */
    static final List STEPS = [
        [
            name           : 'preprocessing',
            requiredColumns: ['patient_id', 'path_to_file', 'is_reference', 'channels'],
            entryColumn    : 'path_to_file',
            qcKinds        : ['preprocess_qc'],
        ],
        [
            name           : 'registration',
            requiredColumns: ['patient_id', 'preprocessed_image', 'is_reference', 'channels'],
            entryColumn    : 'preprocessed_image',
            qcKinds        : ['registration_qc', 'registration_tre', 'seg_qc', 'seg_residuals'],
        ],
        [
            name           : 'segmentation',
            requiredColumns: ['patient_id', 'registered_image', 'is_reference', 'channels'],
            entryColumn    : 'registered_image',
            // Empty by design, not an oversight: SEGMENT and the two property
            // extractors emit only 'versions' and 'size_log', which are
            // UNIVERSAL_QC_KINDS below, so they contribute nothing here. No QC
            // image belongs to segmentation alone — GENERATE_POSTPROCESSING_QC
            // consumes the mask AND the merged quantification table, so it stays
            // postprocessing's.
            qcKinds        : [],
        ],
        [
            name           : 'postprocessing',
            requiredColumns: ['patient_id', 'registered_image', 'is_reference', 'channels'],
            entryColumn    : 'registered_image',
            qcKinds        : ['postprocess_qc'],
        ],
    ]

    // Artifact-stream tags every step contributes, so they belong to no single
    // step's qcKinds. See final_qc.nf's KNOWN_ARTIFACT_KINDS derivation.
    static final List UNIVERSAL_QC_KINDS = ['versions', 'size_log']

    static final List STEP_ORDER = STEPS.collect { it.name }

    static void validateOutdir(String outdir) {
        if (!outdir?.trim()) {
            throw new IllegalArgumentException(
                "Please provide --outdir (the pipeline's output directory). " +
                "Without it every published file lands under a literal 'null/' path.")
        }
    }

    /**
     * Ordering-only check: --stop must not name an earlier step than --start.
     * That both are valid step names is asserted by the schema before this runs.
     */
    static void validateStop(String stop, String start) {
        if (STEP_ORDER.indexOf(stop) < STEP_ORDER.indexOf(start)) {
            throw new IllegalArgumentException("--stop '${stop}' cannot come before --start '${start}'. Pipeline order: ${STEP_ORDER.join(' → ')}")
        }
    }

    static void validateAddCycle(String outdir, String priorOutdir) {
        if (!priorOutdir?.trim()) {
            throw new IllegalArgumentException(
                "mode='add_cycle' requires --prior_outdir pointing at the previous run's --outdir")
        }
        // add_cycle writes its registered-checkpoint CSV via collectFile(storeDir:),
        // which OVERWRITES. If --outdir resolved to the same directory as
        // --prior_outdir, that write would clobber the prior run's complete
        // manifest with add_cycle's partial one, while the postprocessed-checkpoint
        // CSV (untouched by add_cycle) survives — leaving --prior_outdir internally
        // inconsistent and unrecoverable. Compare canonical paths, not raw strings:
        // a trailing slash, a '.', or a symlink must not defeat this check.
        if (outdir?.trim()) {
            def outdirCanonical = new File(outdir).canonicalPath
            def priorOutdirCanonical = new File(priorOutdir).canonicalPath
            if (outdirCanonical == priorOutdirCanonical) {
                def registeredRel = Layout.checkpointCsvRelative(Layout.REGISTERED)
                def postprocessedRel = Layout.checkpointCsvRelative(Layout.POSTPROCESSED)
                throw new IllegalArgumentException(
                    "mode='add_cycle': --outdir must not be the same directory as --prior_outdir " +
                    "('${priorOutdir}'). add_cycle's '${registeredRel}' checkpoint write overwrites " +
                    "in place, which would clobber the prior run's manifest while '${postprocessedRel}' " +
                    "survives untouched, leaving --prior_outdir internally inconsistent. Use a FRESH " +
                    "--outdir for the incremental run, as docs/add_cycle.md describes.")
            }
        }
        // Which checkpoints a prior run must have left behind, and where they live,
        // is Layout's to state — add_cycle.nf reads the very same two files.
        Layout.ADD_CYCLE_CHECKPOINTS.each { step ->
            def rel = Layout.checkpointCsvRelative(step)
            def f = new File(Layout.checkpointCsv(priorOutdir, step))
            if (!f.exists()) {
                throw new FileNotFoundException(
                    "mode='add_cycle': required checkpoint '${rel}' not found under --prior_outdir '${priorOutdir}'. " +
                    "Was the prior run completed through postprocessing?")
            }
        }
    }

    /**
     * Check whether a given pipeline step should run, based on --start and --stop.
     */
    static boolean shouldRun(String targetStep, String start, String stop) {
        def idx = STEP_ORDER.indexOf(targetStep)
        if (idx == -1) throw new IllegalArgumentException("Unknown step: '${targetStep}'. Valid: ${STEP_ORDER}")
        return idx >= STEP_ORDER.indexOf(start) && idx <= STEP_ORDER.indexOf(stop)
    }

    /**
     * Effective registration-QC depth: 0 = none, 1 = DAPI overlay, 2 = + segmentation overlap.
     * Legacy skip_registration_qc=true forces 0. Defined once and shared by
     * registration.nf / add_cycle.nf so the QC gate has a single source of truth.
     */
    static int regQcLevel(Map params) {
        return params.skip_registration_qc ? 0 : (params.reg_qc == null ? 2 : (params.reg_qc as int))
    }

    /**
     * Effective micro-registration depth: 0 = none, 1 = micro-rigid only (refines slide.M),
     * 2 = micro-rigid + micro non-rigid (register_micro). VALIS controls the two passes
     * independently — micro_rigid_registrar_cls for the rigid refinement, the register_micro()
     * call for the non-rigid one — and this ordinal nests them (0 ⊂ 1 ⊂ 2), which forbids the
     * odd "micro non-rigid without micro-rigid" combination. Default 2 (max: micro-rigid +
     * micro non-rigid), matching nextflow.config. Single source of truth for register.nf /
     * warp_seg_qc.nf so the QC can honestly say what the 'rigid' stage means for a given run.
     */
    static int microRegLevel(Map params) {
        return params.reg_micro_reg == null ? 2 : (params.reg_micro_reg as int)
    }

    /**
     * add_cycle does NOT run phenotyping: the incremental subworkflow has no
     * COMPILE_PANEL/PHENOTYPE, and EXPORT_GEOJSON reuses the cell-contours file as a
     * placeholder in the phenotype/model_config slots. If a panel were configured,
     * EXPORT_GEOJSON's arg guard (params.panel_spec || params.panel_model) would activate
     * --phenotypes/--panel_model against that placeholder and mis-classify. Reject the
     * combination at launch rather than silently emit a wrong cells.geojson.
     */
    static void validateAddCyclePhenotyping(Map params) {
        if (params.panel_spec || params.panel_model) {
            throw new IllegalArgumentException(
                "mode='add_cycle' does not support phenotyping. Unset --panel_spec / --panel_model " +
                "for the incremental run — the new cycle inherits the base run's classification.")
        }
    }

    static void validateCompartmentQuant(boolean quantifyCompartments, boolean expanded) {
        if (expanded && !quantifyCompartments) {
            throw new IllegalArgumentException(
                "--expanded_quantification requires --quantify_compartments to be true."
            )
        }
    }

    static List requiredColumnsForStep(String step) {
        def entry = STEPS.find { it.name == step }
        if (!entry) {
            throw new IllegalArgumentException("No column requirements defined for step: ${step}")
        }
        return entry.requiredColumns
    }

    /**
     * The checkpoint-CSV column to read when `step` is the run's entry point
     * (--start). Replaces mirage.nf's hand-written entry_column map.
     */
    static String entryColumnForStep(String step) {
        def entry = STEPS.find { it.name == step }
        if (!entry) {
            throw new IllegalArgumentException("No entry column defined for step: ${step}")
        }
        return entry.entryColumn
    }

    /**
     * Whether `step` is this run's --start step -- i.e. whether the channel for
     * `step` must come from INPUT_CHECK.out.samples rather than the previous
     * step's direct output. Named helper so mirage.nf's routing reads its intent
     * instead of repeating the raw `params.start == '...'` comparison at every
     * branch point.
     */
    static boolean isEntryPoint(Map params, String step) {
        return params.start == step
    }
}
