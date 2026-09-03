/*
========================================================================================
    ResourceReport — where the trace is, and what to say when it is not there
========================================================================================
    main.nf generates the computational-resource report from workflow.onComplete,
    because trace.txt is only finalised at completion. Two things about that handler
    were wrong, and both were invisible:

    1. IT LOOKED IN THE WRONG PLACE. Nextflow resolves `trace.file` --
       "${params.trace_dir}/trace.txt" at nextflow.config:777 -- against launchDir.
       The handler handed the RAW param to cmd.execute(), which resolves a relative
       path against the JVM's working directory. Those coincide only when the
       pipeline was launched from the directory it runs in; a wrapper script, an
       sbatch that cd's, or any harness breaks it.

    2. IT SAID NOTHING WHEN IT FOUND NOTHING. bin/generate_resource_report.py's
       parse_trace returns [] for a path that does not exist and build_html renders
       "Trace data not available", so the script EXITS 0 and main.nf printed
       "Resource report: <path>" over an empty page. A best-effort feature that
       fails invisibly is indistinguishable from one that does not exist.

    Both fixes are pure string arithmetic, which is why they live here rather than in
    main.nf: a script-level function in the entry script has no unit-test surface,
    and tests/lib_probe.nf is the only place lib/*.groovy can be asserted directly
    (nf-test's assertion context cannot see lib/ -- see that file's header).

    All methods are static. Nothing here reads params, touches the filesystem, or
    logs: main.nf owns the log calls, because `log` resolves in a script-level
    function and is unbound almost everywhere else.
========================================================================================
*/

class ResourceReport {

    /** The file Nextflow's trace scope writes inside `trace_dir`. */
    static final String TRACE_FILENAME = 'trace.txt'

    /**
     * Absolute path of the run's trace file.
     *
     * @param traceDir   params.trace_dir, raw. Absolute passes through; relative is
     *                   resolved against launchDir, matching how Nextflow itself
     *                   resolves trace.file.
     * @param launchDir  workflow.launchDir as a String. Required only when traceDir
     *                   is relative.
     * @return           "<resolved trace dir>/trace.txt", normalised.
     * @throws IllegalArgumentException on a blank traceDir, or on a blank launchDir
     *                   with a relative traceDir. Returning the unresolved path in
     *                   either case is what produced the silent miss this class exists
     *                   to remove.
     */
    static String tracePath(String traceDir, String launchDir) {
        if (!traceDir?.trim())
            throw new IllegalArgumentException(
                'ResourceReport.tracePath: trace_dir is required')
        def dir = java.nio.file.Paths.get(traceDir.trim())
        if (!dir.isAbsolute()) {
            if (!launchDir?.trim())
                throw new IllegalArgumentException(
                    "ResourceReport.tracePath: launchDir is required to resolve the " +
                    "relative trace_dir '${traceDir}'")
            dir = java.nio.file.Paths.get(launchDir.trim()).resolve(dir)
        }
        return dir.resolve(TRACE_FILENAME).normalize().toString()
    }

    /**
     * The one line main.nf logs when no resource report can be produced.
     *
     * Always names the path that was looked for, and distinguishes the two reasons
     * it can be absent, because the operator's next action differs completely:
     * a wrong trace_dir is a configuration fix, a disabled trace is a deliberate
     * choice. The previous message ("Could not generate resource report
     * (non-fatal): ...") named neither, and in the common case was not printed at
     * all -- the script exited 0 having found nothing.
     *
     * Never mentions a command-line flag: Nextflow 26 delivers every `--param` as a
     * String, so telling an operator to pass a boolean on the command line is advice
     * that cannot work. -params-file and profiles are the working forms.
     *
     * @param tracePath    the absolute path from tracePath(), for the operator to check
     * @param enableTrace  params.enable_trace, as captured at launch
     */
    static String missingTraceMessage(String tracePath, boolean enableTrace) {
        def why = enableTrace
            ? "enable_trace was ON for this run, so Nextflow should have written it -- " +
              "check that trace_dir is writable and that this run reached completion"
            : "enable_trace was OFF for this run, so Nextflow wrote no trace -- turn it " +
              "on with a -params-file or a profile to collect one"
        return "No resource report was generated: no trace at ${tracePath}. ${why}."
    }
}
