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
    POST-RUN: COMPUTATIONAL RESOURCE REPORT (best-effort)
================================================================================
    trace.txt is only finalized at workflow completion, so the resource report
    is generated here rather than as an in-DAG process. Any failure (no python3
    on the head node, missing trace) logs a warning and never fails the run;
    the script is also runnable by hand against an existing outdir + trace dir.

    Why this is a function called from a handler rather than the handler itself:

      * Nextflow 26's strict parser rejects a top-level `workflow.onComplete { }`
        with "Statements cannot be mixed with script declarations". The handler
        has to be registered from inside the entry `workflow { }` block.
      * A closure registered from there does NOT see the script binding. Inside
        it, `params`, `workflow` and `projectDir` all evaluate to null — under
        the v1 parser as well as v2, so this is a Nextflow scoping fact and not
        a migration artifact. Everything the report needs is therefore captured
        into a plain Map in the workflow body (where those names resolve) and
        handed in as `cfg`.
      * `log` resolves normally inside a script-level function, which is why the
        warn/info calls live here rather than in the handler closure.

    `cfg` deliberately carries only raw params (outdir, trace_dir, project_dir):
    every Layout call stays inside the try below, so a Layout failure is still
    caught and downgraded to a warning exactly as before, rather than being
    hoisted to DAG-construction time where it would fail the run.
*/
def generateResourceReport(Map cfg) {
    if (!cfg.enable_trace) {
        return
    }
    try {
        def script    = "${cfg.project_dir}/bin/generate_resource_report.py"
        def trace_txt = "${cfg.trace_dir}/trace.txt"
        def size_log  = "${Layout.runDir(cfg.outdir, 'size_logs')}/input_sizes.csv"
        def qc_dir    = Layout.runDir(cfg.outdir, 'qc')
        def out_html  = "${qc_dir}/mirage_resource_report.html"
        new File(qc_dir).mkdirs()
        def cmd = [
            'python3', script,
            '--trace', trace_txt,
            '--size-log', size_log,
            '--output', out_html,
            '--native-report', "${cfg.trace_dir}/report.html",
            '--native-timeline', "${cfg.trace_dir}/timeline.html",
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

/*
================================================================================
    RUN MAIN WORKFLOW
================================================================================
*/

workflow {
    main:

    // Captured before MIRAGE() so the handler is registered even if the
    // workflow throws during DAG construction — that is when the previous
    // top-level registration took effect, and onComplete fires on failed runs
    // too.
    def report_cfg = [
        enable_trace: params.enable_trace,
        outdir      : params.outdir,
        trace_dir   : params.trace_dir,
        project_dir : "${projectDir}",
    ]
    workflow.onComplete {
        generateResourceReport(report_cfg)
    }

    MIRAGE()
}
