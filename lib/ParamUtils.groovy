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
     * Which registration adapter does this run use? Single source of truth, shared by
     * REGISTRATION and ADD_CYCLE so an incremental cyclic-IF run takes the same path as a
     * full run instead of being pinned to classic VALIS.
     *
     * Coerces explicitly instead of relying on Groovy truthiness: a -params-file can deliver
     * the STRING "false", and every non-empty String is truthy in Groovy, which would turn
     * the switch silently ON.
     */
    static boolean useDistributedAdapter(Map params) {
        def v = params.reg_distributed_tiling
        if (v == null) return false
        if (v instanceof Boolean) return v
        return Boolean.parseBoolean(v.toString().trim())
    }

    /**
     * Effective registration-QC depth: 0 = none, 1 = DAPI overlay, 2 = + segmentation overlap.
     * Legacy skip_registration_qc=true forces 0. Defined once so the launch-time gate in
     * validateRegistrationPath() cannot drift from the runtime expression it guards
     * (registration.nf / add_cycle.nf both call this).
     */
    static int regQcLevel(Map params) {
        return params.skip_registration_qc ? 0 : (params.reg_qc == null ? 1 : (params.reg_qc as int))
    }

    /**
     * The distributed/low-memory registration path decomposes VALIS into separate processes and
     * therefore produces no single registrar pickle. reg_qc=2 (GeoJSON segmentation-overlap QC)
     * warps polygons THROUGH that pickle, so the two are mutually exclusive. Fail loudly at
     * launch rather than emitting an empty QC channel three hours in.
     *
     * This rejects reg_dist_sub_threshold='auto' too, even though auto routes SOME patients to
     * classic: which patients is decided at runtime by REG_ESTIMATE, so a launch-time gate
     * cannot promise a pickle for any of them.
     */
    static void validateRegistrationPath(Map params) {
        if (!useDistributedAdapter(params)) return
        def level = regQcLevel(params)
        if (level >= 2) {
            throw new IllegalArgumentException(
                "reg_qc=${level} requires the classic VALIS registrar pickle, which the " +
                "distributed path does not produce. Use --reg_qc 1, or set " +
                "--reg_distributed_tiling false."
            )
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
