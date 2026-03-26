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

include { MIRAGE } from './workflows/mirage'

/*
================================================================================
    RUN MAIN WORKFLOW
================================================================================
*/

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
