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

    static void validateSegMethod(String method) {
        def valid = ['stardist', 'instantseg']
        if (!(method in valid)) {
            throw new IllegalArgumentException("Invalid --seg_method '${method}'. Valid values: ${valid}")
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
