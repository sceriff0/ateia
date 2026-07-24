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

/*
================================================================================
    POST-RUN: COMPUTATIONAL RESOURCE REPORT (best-effort)
================================================================================
    trace.txt is only finalized at workflow completion, so the resource report
    is generated here rather than as an in-DAG process. Any failure (no python3
    on the head node, missing trace) logs a warning and never fails the run;
    the script is also runnable by hand against an existing outdir + trace dir.
*/
workflow.onComplete {
    if (!params.enable_trace) {
        return
    }
    try {
        def script    = "${projectDir}/bin/generate_resource_report.py"
        def trace_txt = "${params.trace_dir}/trace.txt"
        def size_log  = "${params.outdir}/size_logs/input_sizes.csv"
        def out_html  = "${params.outdir}/qc/mirage_resource_report.html"
        new File("${params.outdir}/qc").mkdirs()
        def cmd = [
            'python3', script,
            '--trace', trace_txt,
            '--size-log', size_log,
            '--output', out_html,
            '--native-report', "${params.trace_dir}/report.html",
            '--native-timeline', "${params.trace_dir}/timeline.html",
        ]
        def proc = cmd.execute()
        proc.waitForProcessOutput(System.out, System.err)
        if (proc.exitValue() == 0) {
            log.info "Resource report: ${out_html}"
        } else {
            log.warn "Resource report generation exited ${proc.exitValue()} (non-fatal)."
        }
    } catch (Exception e) {
        log.warn "Could not generate resource report (non-fatal): ${e.message}"
    }
}
