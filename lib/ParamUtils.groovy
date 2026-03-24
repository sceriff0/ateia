class ParamUtils {

    static void validateStep(String step) {
        def valid = ['preprocessing', 'registration', 'postprocessing']
        if (!(step in valid)) {
            throw new IllegalArgumentException("Invalid --step '${step}'. Valid values: ${valid}")
        }
    }

    static void validateRegistrationMethod(String method) {
        def valid = ['valis', 'valis_pairs','gpu', 'cpu', 'cpu_tiled']
        if (!(method in valid)) {
            throw new IllegalArgumentException("Invalid --registration_method '${method}'. Valid values: ${valid}")
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
            .replaceAll(/[\[\]']/, '')
            .tokenize(',')
            .collect { it.trim() }
            .findAll { it }
    }
}
