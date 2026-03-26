import nextflow.util.MemoryUnit
import nextflow.util.Duration

class ResourceUtils {

    /**
     * Ensure resources don't exceed maximum limits defined in params.
     * Called from process resource closures in config files.
     */
    static check_max(obj, type, params) {
        if (type == 'memory') {
            try {
                def max = params.max_memory as MemoryUnit
                return obj.compareTo(max) == 1 ? max : obj
            } catch (all) {
                println "WARNING: Max memory '${params.max_memory}' is not valid"
                return obj
            }
        } else if (type == 'time') {
            try {
                def max = params.max_time as Duration
                return obj.compareTo(max) == 1 ? max : obj
            } catch (all) {
                println "WARNING: Max time '${params.max_time}' is not valid"
                return obj
            }
        } else if (type == 'cpus') {
            try {
                return Math.min(obj, params.max_cpus as int)
            } catch (all) {
                println "WARNING: Max cpus '${params.max_cpus}' is not valid"
                return obj
            }
        }
    }
}
