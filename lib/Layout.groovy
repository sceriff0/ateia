/*
 * Layout - the pipeline's one description of WHERE A FILE LANDS under --outdir.
 *
 * `conf/modules.config` decides where each process actually publishes. Everything
 * else that needs to KNOW that answer - the checkpoint CSVs that record published
 * paths for a later `--start`, the add_cycle reader that opens a prior run's
 * checkpoints, the validator that asserts they exist, the onComplete resource
 * report - used to restate the rule by hand. Six independent copies, kept in
 * agreement only by eye:
 *
 *   1. subworkflows/local/preprocess.nf   "<outdir>/<pid>/preprocessed/<name>"
 *                                          + collectFile storeDir "<outdir>/csv"
 *   2. subworkflows/local/registration.nf "<outdir>/<pid>/registered/<rel>" and the
 *                                          work-hash heuristic that derived <rel>
 *   3. subworkflows/local/postprocess.nf   five "<outdir>/<pid>/<kind>/<name>"
 *                                          templates (geojson x2, quantification,
 *                                          segmentation, pyramid)
 *   4. subworkflows/local/add_cycle.nf     "<prior_outdir>/csv/registered.csv" and
 *                                          "<prior_outdir>/csv/postprocessed.csv"
 *   5. lib/ParamUtils.groovy               the same two relative paths again, as
 *                                          literals in the add_cycle precondition
 *   6. workflows/mirage.nf / main.nf       "<prior_outdir>/csv/postprocessed.csv",
 *                                          "<outdir>/size_logs/...", "<outdir>/qc"
 *
 * All six now come through here. When the published layout changes, change it in
 * conf/modules.config and in this class - and nowhere else. DO NOT re-scatter a
 * path template into a workflow file: the whole point of this class is that the
 * next contributor has something to ask instead of something to re-derive.
 *
 * SCOPE. This class describes the layout; it does not enforce it. The publishDir
 * blocks in conf/modules.config are deliberately NOT routed through here (they run
 * in a different evaluation context and rewriting them would change publish
 * behaviour). So this class must be kept in agreement with them by hand - but that
 * is one agreement to maintain instead of six.
 *
 * All methods are static and take `outdir` as an ARGUMENT; nothing here reads
 * `params`. lib/*.groovy is called from many contexts (workflow closures,
 * onComplete, unit tests) and a static class reaching into `params` is neither
 * testable nor safe. Follows lib/ParamUtils.groovy, which receives params the
 * same way.
 */
class Layout {

    /* ------------------------------------------------------------------ *
     * Checkpoint CSVs
     * ------------------------------------------------------------------ */

    /** Run-level directory (relative to --outdir) holding the checkpoint CSVs. */
    static final String CSV_DIR = 'csv'

    /**
     * The checkpoint step names. These are the CSV basenames AND the `--start` /
     * `--stop` vocabulary minus the "-ing" (`preprocessing` writes `preprocessed.csv`),
     * which is exactly the sort of near-miss that produced the hardcoded literals this
     * class replaces. Use the constants, never the strings.
     */
    static final String PREPROCESSED  = 'preprocessed'
    static final String REGISTERED    = 'registered'
    static final String POSTPROCESSED = 'postprocessed'

    static final List<String> CHECKPOINT_STEPS =
        [PREPROCESSED, REGISTERED, POSTPROCESSED].asImmutable()

    /** The two checkpoints a `mode='add_cycle'` run reads out of --prior_outdir. */
    static final List<String> ADD_CYCLE_CHECKPOINTS =
        [REGISTERED, POSTPROCESSED].asImmutable()

    private static String requireStep(String step) {
        if (!CHECKPOINT_STEPS.contains(step))
            throw new IllegalArgumentException(
                "Unknown checkpoint step: '${step}'. Valid: ${CHECKPOINT_STEPS}")
        return step
    }

    private static String requireOutdir(def outdir) {
        def s = outdir?.toString()
        if (!s?.trim())
            throw new IllegalArgumentException(
                "Layout needs an output directory; got ${outdir == null ? 'null' : "'${outdir}'"}. " +
                "Pass params.outdir (or params.prior_outdir) explicitly.")
        return stripTrailingSlash(s)
    }

    private static String stripTrailingSlash(String s) {
        return s.length() > 1 && s.endsWith('/') ? s[0..-2] : s
    }

    /** `<outdir>/csv` - the collectFile storeDir every checkpoint CSV is written to. */
    static String checkpointDir(def outdir) {
        return "${requireOutdir(outdir)}/${CSV_DIR}"
    }

    /** `<step>.csv` - the collectFile `name:` for a checkpoint. */
    static String checkpointCsvName(String step) {
        return "${requireStep(step)}.csv"
    }

    /** `csv/<step>.csv` - outdir-relative, for messages and existence checks. */
    static String checkpointCsvRelative(String step) {
        return "${CSV_DIR}/${checkpointCsvName(step)}"
    }

    /** `<outdir>/csv/<step>.csv` - the absolute checkpoint path. */
    static String checkpointCsv(def outdir, String step) {
        return "${requireOutdir(outdir)}/${checkpointCsvRelative(step)}"
    }

    /* ------------------------------------------------------------------ *
     * Published directories
     * ------------------------------------------------------------------ */

    /**
     * `<outdir>/<patient_id>/<kind>` - the per-patient publish root.
     *
     * `kind` is the leaf conf/modules.config publishes into: 'preprocessed',
     * 'registered', 'segmentation', 'quantification', 'geojson', 'pyramid', ...
     */
    static String patientDir(def outdir, def patientId, String kind) {
        if (!patientId?.toString()?.trim())
            throw new IllegalArgumentException("Layout.patientDir: patient_id is required")
        if (!kind?.trim())
            throw new IllegalArgumentException("Layout.patientDir: kind is required")
        return "${requireOutdir(outdir)}/${patientId}/${kind}"
    }

    /** `<outdir>/<kind>` - a run-level (not per-patient) publish directory. */
    static String runDir(def outdir, String kind) {
        if (!kind?.trim())
            throw new IllegalArgumentException("Layout.runDir: kind is required")
        return "${requireOutdir(outdir)}/${kind}"
    }

    /**
     * `<outdir>/<patient_id>/<kind>/<basename>` - where `file` will be published.
     *
     * Only the BASENAME is used. Callers whose producer emits into a subdirectory of
     * its task directory (currently only registration) want
     * publishedPathWithProducerSubdir instead.
     */
    static String publishedPath(def outdir, def patientId, String kind, def file) {
        return "${patientDir(outdir, patientId, kind)}/${basename(file)}"
    }

    /**
     * `<outdir>/<patient_id>/<kind>/[<producer subdir>/]<basename>`.
     *
     * Some producers write into a named subdirectory of their task work directory and
     * publish with a `pattern:` that carries that subdirectory along, so the published
     * path keeps it:
     *
     *   REGISTER (VALIS)  pattern 'registered_slides/*_registered.ome.tiff'
     *                     -> <outdir>/<pid>/registered/registered_slides/<name>
     *   TILED_REGISTER /  pattern 'registered/*_registered.ome.tiff'
     *   TILED_STITCH      -> <outdir>/<pid>/registered/registered/<name>
     *   passthrough       reference slides emitted straight into the task dir
     *                     -> <outdir>/<pid>/registered/<name>
     *
     * See producerSubdir for how the subdirectory is recovered, and why.
     */
    static String publishedPathWithProducerSubdir(def outdir, def patientId, String kind, def file) {
        def sub = producerSubdir(file)
        def rel = sub ? "${sub}/${basename(file)}" : basename(file)
        return "${patientDir(outdir, patientId, kind)}/${rel}"
    }

    /**
     * The subdirectory a producer emitted `file` into, or '' when it emitted straight
     * into its task work directory.
     *
     * THIS IS A HEURISTIC, AND IT IS DELIBERATELY HERE RATHER THAN IN A WORKFLOW FILE.
     * A Nextflow task directory is a 32-hex-character hash, so a staged output whose
     * PARENT is such a hash sits at the top of its task directory and publishes under
     * its bare name; a parent that is not a hash ('registered_slides', 'registered') is
     * a real subdirectory that publishDir's `pattern:` carries into the published path.
     *
     * It lives here because the alternative was worse, not because it is elegant:
     *
     *   - Passing the subdirectory in from the caller means threading producer identity
     *     through both registration adapters AND the single-slide passthrough branch;
     *     within one adapter (tiled) the answer differs per item, since the reference
     *     passes through unregistered while moving slides come out of 'registered/'.
     *   - Deriving it from the file's position under workflow.workDir (strip the workDir
     *     prefix and the two hash components) is cleaner, but silently yields the wrong
     *     answer for a file that did NOT come from the work directory - which is exactly
     *     what a `--start registration` run reads out of a published checkpoint CSV.
     *
     * Either replacement would change an emitted path in a case no stub run exercises.
     * The published-path contract is frozen (the checkpoint CSVs are a user-visible
     * restart contract), so the heuristic stays - but as ONE copy, with the five
     * hand-written path templates that used to accompany it now gone.
     */
    static String producerSubdir(def file) {
        def path = resolve(file)
        def parent = path?.parent?.name?.toString() ?: ''
        return isWorkHash(parent) ? '' : parent
    }

    /** True for a Nextflow task-directory name (32 lowercase hex characters). */
    static boolean isWorkHash(String name) {
        return name != null && name.length() == 32 && name.matches(/^[0-9a-f]{32}$/)
    }

    /* ------------------------------------------------------------------ *
     * Internals
     * ------------------------------------------------------------------ */

    /**
     * Nextflow unwraps a single-element output glob to a bare Path but leaves a
     * multi-element one a List; callers hand us whichever they got.
     */
    private static def resolve(def file) {
        return file instanceof List ? (file ? file[0] : null) : file
    }

    private static String basename(def file) {
        def path = resolve(file)
        if (path == null)
            throw new IllegalArgumentException("Layout: no file given")
        return path.name.toString()
    }
}
