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
    static final String SEGMENTED     = 'segmented'
    static final String POSTPROCESSED = 'postprocessed'

    static final List<String> CHECKPOINT_STEPS =
        [PREPROCESSED, REGISTERED, SEGMENTED, POSTPROCESSED].asImmutable()

    /** The two checkpoints a `mode='add_cycle'` run reads out of --prior_outdir. */
    static final List<String> ADD_CYCLE_CHECKPOINTS =
        [REGISTERED, POSTPROCESSED].asImmutable()

    private static String requireStep(String step) {
        if (!CHECKPOINT_STEPS.contains(step))
            throw new Layout.UnknownStepException(
                "Layout: unknown checkpoint step: '${step}'. Valid: ${CHECKPOINT_STEPS}")
        return step
    }

    /**
     * Thrown when a caller names a step that is not in {@link #CHECKPOINT_STEPS}. Its
     * own class (rather than a bare {@code IllegalArgumentException}) so a caller can
     * tell, from the exception type alone, that Layout — not Checkpoint, which throws
     * the same message text for the same reason — is the one that rejected the name.
     */
    static class UnknownStepException extends IllegalArgumentException {
        UnknownStepException(String message) { super(message) }
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

    /*
     * ------------------------------------------------------------------
     * Per-patient checkpoint FRAGMENTS
     * ------------------------------------------------------------------
     * `<outdir>/csv/<step>.parts/<patient_id>.csv` - one patient's rows of one
     * checkpoint, published by WRITE_CHECKPOINT_FRAGMENT the moment that patient's
     * step finishes.
     *
     * WHY THE AGGREGATE IS NOT ENOUGH. csv/<step>.csv is written by collectFile(),
     * an OPERATOR: it writes once, when the upstream channel closes, i.e. when the
     * LAST patient finishes. Between "patient 3 of 20 finished" and "patient 20
     * finished" the only record of patients 1-3's published outputs lives in the
     * driver JVM's memory. A walltime kill on the head job, an OOM, or a node
     * failure in that window loses the index while the work itself sits published
     * on disk. The fragments close that window: each is a complete, standalone
     * checkpoint CSV for one patient - same header, same rows, same grammar (see
     * lib/Checkpoint.groovy, which owns all three) - so the aggregate is the
     * convenient artifact rather than the only record.
     *
     * THE SUFFIX IS MIRRORED IN conf/modules.config. WRITE_CHECKPOINT_FRAGMENT's
     * publishDir has to spell `${params.outdir}/csv/${step}.parts` by hand, because
     * conf/*.config cannot see lib/*.groovy classes (it fails SILENTLY - see
     * CLAUDE.md). That is the same hand-maintained agreement every other publish
     * path in this class has with the config, and tests/test_layout.py asserts this
     * one mechanically rather than by eye.
     *
     * NOT A SEPARATE `kind`. PUBLISHED_KINDS is the per-PATIENT publish vocabulary
     * (`<outdir>/<pid>/<kind>`); fragments are run-level, under csv/, keyed by
     * patient in the FILENAME. patientDir() must not be asked for them.
     */

    /** The directory-name suffix marking a checkpoint's per-patient fragments. */
    static final String FRAGMENT_DIR_SUFFIX = '.parts'

    /** `<step>.parts` - the csv/-relative directory holding a step's fragments. */
    static String checkpointFragmentDirName(String step) {
        return "${requireStep(step)}${FRAGMENT_DIR_SUFFIX}"
    }

    /** `<outdir>/csv/<step>.parts` - the absolute fragment directory. */
    static String checkpointFragmentDir(def outdir, String step) {
        return "${checkpointDir(outdir)}/${checkpointFragmentDirName(step)}"
    }

    /** `<patient_id>.csv` - the fragment's basename, and the process's output name. */
    static String checkpointFragmentName(def patientId) {
        if (!patientId?.toString()?.trim())
            throw new IllegalArgumentException("Layout.checkpointFragmentName: patient_id is required")
        return "${patientId}.csv"
    }

    /** `csv/<step>.parts/<patient_id>.csv` - outdir-relative, for messages and checks. */
    static String checkpointFragmentRelative(String step, def patientId) {
        return "${CSV_DIR}/${checkpointFragmentDirName(step)}/${checkpointFragmentName(patientId)}"
    }

    /** `<outdir>/csv/<step>.parts/<patient_id>.csv` - the absolute fragment path. */
    static String checkpointFragment(def outdir, String step, def patientId) {
        return "${requireOutdir(outdir)}/${checkpointFragmentRelative(step, patientId)}"
    }

    /* ------------------------------------------------------------------ *
     * Published directories
     * ------------------------------------------------------------------ */

    /**
     * The closed vocabulary of per-patient publish leaves — every `kind` any caller may
     * pass to patientDir()/publishedPath(), and every leaf conf/modules.config publishes
     * a per-patient artifact into.
     *
     * WHY A CLOSED LIST. `kind` was a free-form String. Three of the ~14 kinds in use had
     * constants (PREPROCESSED, REGISTERED, POSTPROCESSED); the rest were bare literals at
     * the call site ('geojson', 'quantification', 'segmentation', 'pyramid'), so a typo
     * produced a checkpoint row naming a directory that does not exist — green run,
     * unresolvable path. tests/test_layout.py checks this list against
     * conf/modules.config in BOTH directions: a kind here that the config never publishes
     * into is dead, and a per-patient publish leaf in the config that is absent here is a
     * path with no owner.
     *
     * 'spatialdata' is deliberately NOT here: EXPORT_SPATIALDATA publishes to the bare
     * patient root (`${params.outdir}/${meta.patient_id}`, no trailing leaf) because its
     * .zarr output already carries `spatialdata/` from the process's own `pattern:` —
     * publishing it under a `.../spatialdata` kind directory would double-nest it. The
     * `<outdir>/<pid>/spatialdata/` directory does exist on disk; what does NOT exist is
     * a `path:` in conf/modules.config that names 'spatialdata' as a segment, so there is
     * nothing for a caller to ask Layout for.
     *
     * Ordered as the pipeline produces them, not alphabetically.
     */
    static final List<String> PUBLISHED_KINDS = [
        'converted',
        PREPROCESSED,
        REGISTERED,
        'segmentation',
        'cell_properties',
        'split_channels',
        'quantify',
        'quantification',
        'geojson',
        'pyramid',
    ].asImmutable()

    /** Reject an unknown publish kind at the call site rather than at path-resolution time. */
    static String requireKind(String kind) {
        if (!PUBLISHED_KINDS.contains(kind))
            throw new IllegalArgumentException(
                "Unknown published kind: '${kind}'. Valid: ${PUBLISHED_KINDS}")
        return kind
    }

    /**
     * `<outdir>/<patient_id>/<kind>` - the per-patient publish root.
     *
     * `kind` is the leaf conf/modules.config publishes into — one of the closed set in
     * PUBLISHED_KINDS above (enforced by requireKind() below), not an open-ended list.
     */
    static String patientDir(def outdir, def patientId, String kind) {
        requireKind(kind)
        if (!patientId?.toString()?.trim())
            throw new IllegalArgumentException("Layout.patientDir: patient_id is required")
        return "${requireOutdir(outdir)}/${patientId}/${kind}"
    }

    /** `<outdir>/<kind>` - a run-level (not per-patient) publish directory. */
    static String runDir(def outdir, String kind) {
        if (!kind?.trim())
            throw new IllegalArgumentException("Layout.runDir: kind is required")
        return "${requireOutdir(outdir)}/${kind}"
    }

    /**
     * `<outdir>/<patient_id>/<kind>/[<producer subdir>/]<basename>` - where `file`
     * will be published.
     *
     * A process that writes into a named subdirectory of its task directory and
     * publishes with a `pattern:` that names that subdirectory gets the subdirectory
     * carried into the published path. conf/modules.config does this in three places:
     *
     *   REGISTER (VALIS)  pattern 'registered_slides/*_registered.ome.tiff'
     *                     -> <outdir>/<pid>/registered/registered_slides/<name>
     *   TILED_STITCH      pattern 'registered/*_registered.ome.tiff'
     *                     -> <outdir>/<pid>/registered/registered/<name>
     *   EXPORT_GEOJSON    output path("export/cells.geojson"), published bare
     *                     -> <outdir>/<pid>/geojson/export/cells.geojson
     *
     * So this is NOT an option a caller opts into - a caller that forgets records a
     * path that does not exist, which is exactly how csv/postprocessed.csv came to
     * name <pid>/geojson/cells.geojson for two releases. Preserving the producer
     * subdirectory is the DEFAULT and the only behaviour; there is deliberately no
     * basename-only variant to pick by mistake. tests/checkpoint_manifest.nf.test
     * asserts the consequence: no checkpoint row ever names a missing file.
     */
    static String publishedPath(def outdir, def patientId, String kind, def file) {
        def sub = producerSubdir(file)
        def rel = sub ? "${sub}/${basename(file)}" : basename(file)
        return "${patientDir(outdir, patientId, kind)}/${rel}"
    }

    /**
     * Like {@link #publishedPath}, but tolerant of a `file` that is ALREADY an
     * absolute path to a previously published artifact rather than a fresh output in
     * this run's own task directory -- e.g. a mask read back from an earlier
     * checkpoint CSV via `--start postprocessing`, which segmentation.nf's
     * READ_SEGMENTED_CHECKPOINT hands to POSTPROCESSING as a plain `file(row.cell_mask)`,
     * or a registered image read back via `--start segmentation`'s INPUT_CHECK.
     *
     * Applying `publishedPath` unconditionally to such a file mis-records it. Two
     * failure shapes, both fixed by the same check:
     *
     *   - A flat artifact (no producer subdirectory, e.g. SEGMENT's cell_mask):
     *     double-nests the kind directory. `producerSubdir` sees a parent named e.g.
     *     'segmentation' (not a task hash) and treats it as a producer subdirectory
     *     to preserve, yielding `<outdir>/<pid>/segmentation/segmentation/<name>`
     *     instead of the already-correct path the file already sits at.
     *   - An artifact published one level down (e.g. REGISTER's `registered_slides/`):
     *     the file's immediate parent is a producer-subdirectory NAME in BOTH the
     *     fresh-this-run case and the already-published case (a prior run's
     *     `.../registered/registered_slides/<name>` has a parent named
     *     'registered_slides' too), so a one-level `isTaskDir(path.parent)` check
     *     cannot tell them apart. It reconstructs the path against the WRONG outdir
     *     (this run's, not wherever the file actually is) and, when the recorded
     *     `kind` differs from the file's real one (e.g. a passthrough slide
     *     published under 'preprocessed/' but asked about as 'registered'), against
     *     the wrong kind too.
     *
     * {@link #isUnderTaskDir} checks the immediate parent AND grandparent, which
     * covers every producer-subdirectory depth this method is CURRENTLY ASKED
     * ABOUT (one level: `registered_slides/`, `export/`, `nuclei/` -- see
     * {@link #publishedPath}'s docblock). It is NOT a claim that one level is the
     * most this codebase's publishDir blocks ever use -- REGISTER's own
     * `pattern: "preprocessed/data/*.csv"` (conf/modules.config) is two levels, and
     * nothing stops a future emit from being deeper still. That two-level emit is
     * harmless today only because nothing ever hands it to Layout. If one ever did,
     * a fresh two-level output would fall through `isUnderTaskDir` to the as-is
     * branch and get recorded VERBATIM AS ITS WORK-DIRECTORY PATH -- silently
     * wrong, not loudly. Widen this check (or add a depth-aware variant) before
     * routing a second-level producer subdirectory through `publishedOrAsIs`.
     *
     * When neither the parent nor grandparent is a task directory, `file` is
     * already an absolute published path from wherever it actually lives (this run
     * or a prior one) and is returned as-is, whole -- reconstructing it against THIS
     * run's outdir would be wrong precisely because it is not this run's file.
     */
    static String publishedOrAsIs(def outdir, def patientId, String kind, def file) {
        def path = resolve(file)
        if (path == null)
            throw new IllegalArgumentException("Layout.publishedOrAsIs: no file given")
        return isUnderTaskDir(path.parent)
            ? publishedPath(outdir, patientId, kind, path)
            : path.toString()
    }

    /**
     * Where a slide that was NOT registered will be published. A single-slide patient
     * has nothing to register, so registration.nf branches its reference straight
     * through (`ch_passthrough`); the tiled backend does the same for every patient's
     * reference, which defines the frame and is never warped. NOTHING publishes those
     * into `<pid>/registered/` - no process emitted them - so recording them there
     * names a file that does not exist. Verified against a stub run: a single-slide
     * patient's output tree contains no `P001/registered/` directory at all.
     *
     * The slide is still published, just by whoever produced it:
     *
     *   produced by PREPROCESS this run  -> <outdir>/<pid>/preprocessed/<name>
     *   supplied by a `--start registration` samplesheet -> already an absolute
     *                                       path to an existing file; record it as is
     *
     * A thin, PREPROCESSED-pinned wrapper over {@link #publishedOrAsIs} -- this
     * predates it and is kept as the named entry point every existing caller uses,
     * not because the logic differs.
     */
    static String passthroughPath(def outdir, def patientId, def file) {
        return publishedOrAsIs(outdir, patientId, PREPROCESSED, file)
    }

    /**
     * The subdirectory a producer emitted `file` into, or '' when it emitted straight
     * into its task directory.
     *
     * This used to be a LENGTH heuristic ("a Nextflow work dir is a 32-hex-char hash")
     * living in subworkflows/local/registration.nf, and it was WRONG: Nextflow splits
     * the hash as `<work>/<2 hex>/<30 hex>`, so a task directory name is 30 characters,
     * never 32, and the test never once fired. Nobody noticed because on the VALIS path
     * every registered file really does have a `registered_slides/` parent. The one
     * case the test existed to catch - a file emitted bare into its task dir - was
     * silently mis-recorded as `<pid>/registered/<30-hex-hash>/<name>`.
     * tests/checkpoint_manifest.nf.test found it on its first run.
     *
     * It is now a STRUCTURAL test (see isTaskDir), not a length guess.
     */
    static String producerSubdir(def file) {
        def path = resolve(file)
        def parent = path?.parent
        if (parent == null) return ''
        return isTaskDir(parent) ? '' : (parent.name?.toString() ?: '')
    }

    /**
     * True when `dir` is a Nextflow task directory: `<workDir>/<2 hex>/<30 hex>`.
     *
     * Both halves are checked. The name alone would misclassify a genuine output
     * subdirectory that happened to be hex-shaped; requiring the two-character hex
     * parent as well makes a false positive essentially impossible. The width is
     * accepted as 30..32 rather than pinned at 30 so a future Nextflow that widens
     * the hash keeps working - the two-char parent is the load-bearing half.
     */
    static boolean isTaskDir(def dir) {
        if (dir == null) return false
        def name = dir.name?.toString()
        if (!name || !name.matches(/^[0-9a-f]{30,32}$/)) return false
        def prefix = dir.parent?.name?.toString()
        return prefix != null && prefix.matches(/^[0-9a-f]{2}$/)
    }

    /**
     * True when `dir` IS a task directory, or is a producer subdirectory ONE level
     * below one (REGISTER's `registered_slides/`, EXPORT_GEOJSON's `export/`,
     * EXTRACT_NUCLEI_PROPERTIES's `nuclei/` -- every depth {@link #publishedOrAsIs}
     * is CURRENTLY ASKED ABOUT; see {@link #publishedPath}'s docblock). This is not
     * the deepest nesting this codebase's publishDir blocks use in general --
     * REGISTER's `pattern: "preprocessed/data/*.csv"` is two levels -- it is the
     * deepest that has ever needed to survive THIS check. A future two-level
     * caller would silently fall through to the as-is branch and get its live
     * work-directory path recorded as though it were already published; widen this
     * check first if that ever happens (see {@link #publishedOrAsIs}'s docblock).
     *
     * A single-level `isTaskDir(dir)` check cannot tell a FRESH one-level-nested
     * output from an ALREADY-PUBLISHED path with the same producer-subdirectory
     * name: `.../registered/registered_slides/<name>` (a prior run's published file)
     * and `<workDir>/xx/yyyy.../registered_slides/<name>` (this run's live task
     * output) both have a parent literally named 'registered_slides', which never
     * matches the hex-hash pattern either way. Checking the grandparent too is what
     * {@link #publishedOrAsIs} needs to tell "fresh, needs publishedPath" from
     * "already published elsewhere, use as-is" for a channel that can carry either,
     * depending on the run's entry point.
     */
    static boolean isUnderTaskDir(def dir) {
        return dir != null && (isTaskDir(dir) || isTaskDir(dir.parent))
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
