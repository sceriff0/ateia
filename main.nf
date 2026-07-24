#!/usr/bin/env nextflow

nextflow.enable.dsl = 2

/*
================================================================================
    MIRAGE WSI Processing Pipeline
================================================================================
    Preprocessing, Registration, Segmentation, Quantification, GeoJSON export
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
