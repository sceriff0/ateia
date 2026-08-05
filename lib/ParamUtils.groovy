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

    static final List STEP_ORDER = ['preprocessing', 'registration', 'postprocessing']

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

    static void validateAddCycle(String priorOutdir) {
        if (!priorOutdir?.trim()) {
            throw new IllegalArgumentException(
                "mode='add_cycle' requires --prior_outdir pointing at the previous run's --outdir")
        }
        ['csv/registered.csv', 'csv/postprocessed.csv'].each { rel ->
            def f = new File("${priorOutdir}/${rel}")
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
        def requirements = [
            preprocessing : ['patient_id','path_to_file','is_reference','channels'],
            registration  : ['patient_id','preprocessed_image','is_reference','channels'],
            postprocessing: ['patient_id','registered_image','is_reference','channels']
        ]

        if (!requirements.containsKey(step)) {
            throw new IllegalArgumentException("No column requirements defined for step: ${step}")
        }

        return requirements[step]
    }

    /**
     * Parse a parameter that may be a List, a stringified list like "['a','b']", or a comma-separated string.
     * Returns a cleaned List<String> with empty entries removed.
     */
    static List<String> parseListParam(param) {
        if (param instanceof List) {
            return param.collect { it.toString().trim() }.findAll { it }
        }
        return (param ?: '').toString()
            .replaceAll(/^\[|\]$/, '')    // strip outer brackets only
            .tokenize(',')
            .collect { it.trim().replaceAll(/^['"]|['"]$/, '') }  // strip surrounding quotes per element
            .findAll { it }
    }
}
