/*
 * ParamUtils - validation helpers for the pipeline's step-routing parameters.
 *
 * Checks that --start / --stop name valid steps and are in the right order,
 * against the canonical preprocessing -> registration -> postprocessing sequence.
 */
class ParamUtils {

    static final List STEP_ORDER = ['preprocessing', 'registration', 'postprocessing']

    static void validateStart(String start) {
        if (!(start in STEP_ORDER)) {
            throw new IllegalArgumentException("Invalid --start '${start}'. Valid values: ${STEP_ORDER}")
        }
    }

    static void validateStop(String stop, String start) {
        if (!(stop in STEP_ORDER)) {
            throw new IllegalArgumentException("Invalid --stop '${stop}'. Valid values: ${STEP_ORDER}")
        }
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

    static void validateRegistrationMethod(String method) {
        def valid = ['valis']
        if (!(method in valid)) {
            throw new IllegalArgumentException("Invalid --registration_method '${method}'. Valid values: ${valid}")
        }
    }

    static void validateRegQc(int level) {
        def valid = [0, 1, 2]
        if (!(level in valid)) {
            throw new IllegalArgumentException("Invalid --reg_qc '${level}'. Valid values: ${valid} (0=none, 1=DAPI overlay, 2=+segmentation QC)")
        }
    }

    /**
     * Read a boolean switch without trusting Groovy truthiness: a -params-file (or --flag false
     * on the command line) can deliver the STRING "false", and every non-empty String is truthy
     * in Groovy — which would turn the switch silently ON.
     */
    static boolean boolParam(Map params, String name) {
        def v = params[name]
        if (v == null) return false
        if (v instanceof Boolean) return v
        return Boolean.parseBoolean(v.toString().trim())
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
     * odd "micro non-rigid without micro-rigid" combination. Default 0 preserves the pipeline's
     * historical behaviour (micro off). Single source of truth for register.nf / warp_seg_qc.nf
     * so the QC can honestly say what the 'rigid' stage means for a given run.
     */
    static int microRegLevel(Map params) {
        return params.micro_reg == null ? 0 : (params.micro_reg as int)
    }

    static void validateMicroReg(int level) {
        def valid = [0, 1, 2]
        if (!(level in valid)) {
            throw new IllegalArgumentException("Invalid --micro_reg '${level}'. Valid values: ${valid} (0=none, 1=micro-rigid only, 2=micro-rigid + micro non-rigid)")
        }
    }

    static void validateSegMethod(String method) {
        def valid = ['stardist', 'instantseg', 'cellsam']
        if (!(method in valid)) {
            throw new IllegalArgumentException("Invalid --seg_method '${method}'. Valid values: ${valid}")
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
