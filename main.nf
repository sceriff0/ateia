#!/usr/bin/env nextflow

nextflow.enable.dsl = 2

/*
================================================================================
    MIRAGE WSI Processing Pipeline
================================================================================
    Preprocessing, Registration, Segmentation, Quantification, Phenotyping
    https://github.com/sceriff0/mirage
================================================================================
*/

/*
================================================================================
    RESOURCE LIMIT FUNCTION
================================================================================
*/

def check_max(obj, type) {
    if (type == 'memory') {
        try {
            if (obj.compareTo(params.max_memory as nextflow.util.MemoryUnit) == 1)
                return params.max_memory as nextflow.util.MemoryUnit
            else
                return obj
        } catch (all) {
            println "WARNING: Max memory '${params.max_memory}' is not valid"
            return obj
        }
    } else if (type == 'time') {
        try {
            if (obj.compareTo(params.max_time as nextflow.util.Duration) == 1)
                return params.max_time as nextflow.util.Duration
            else
                return obj
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

/*
================================================================================
    RUN MAIN WORKFLOW
================================================================================
*/

include { MIRAGE } from './workflows/mirage'

workflow {
    MIRAGE()
}

/*
================================================================================
    COMPLETION HANDLERS
================================================================================
*/

workflow.onComplete {
    if (workflow.success) {
        log.info "Pipeline completed successfully!"
    } else {
        log.error "Pipeline failed - work directory preserved for debugging"
    }
}
