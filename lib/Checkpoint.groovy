/*
========================================================================================
    Checkpoint — the checkpoint CSV's SCHEMA owner
========================================================================================
    lib/Layout.groovy owns WHERE a checkpoint CSV lives (`<outdir>/csv/<step>.csv`).
    This class owns WHAT IS IN IT: the ordered column list, the header line, and the
    row builder.

    WHY THIS IS A SEPARATE OWNER. Before this class the header was a `seed:` string
    literal written out by hand in three subworkflows (preprocess.nf,
    registered_checkpoint.nf, postprocess.nf) and the same column names were
    re-stated by hand in the readers (add_cycle.nf). registered_checkpoint.nf's own
    header comment claimed the row format "lives in exactly one place"; it lived in
    five. A writer could transpose two columns, or a reader could name a column the
    writer never emitted, and nothing failed — the run stayed green and the
    checkpoint recorded paths that did not resolve.

    THE ROW BUILDER IS THE POINT. `row(step, map)` orders values by the DECLARED
    column list, so insertion order at the call site is irrelevant, and it throws on
    a missing or unknown KEY rather than silently omitting a column. A column whose
    key is absent from the map would emit an empty field, and an empty field in a
    checkpoint row is a path that does not exist — the failure mode the
    postprocessing checkpoint manifest shipped with for two releases. This checks key
    presence only: a key present with a `null` (or empty-string) VALUE is not caught
    here and is written through as-is (e.g. the literal text `null`) — unchanged from
    the hand-written GStrings this class replaced.

    SCOPE. Like Layout, this class never reads `params`: every method takes what it
    needs as an argument, so it is callable from an onComplete handler and from
    tests/lib_probe.nf. All methods are static.

    NOT AN ESCAPE HATCH FOR CSV QUOTING. Values are joined with a bare comma, exactly
    as the three writers did before. No published value contains a comma today
    (`channels` uses `|` as its separator precisely for this reason). If that ever
    changes, quoting belongs here — in one place — which is the other reason this
    class exists.

    EMPTY VALUES. A column whose artifact a run did not produce carries the empty
    string, not a missing key: `nucleus_contours` is empty when --quantify_compartments
    is false (EXTRACT_NUCLEI_PROPERTIES does not run at all in that case). `nuclei_mask`
    is NOT similarly gated -- SEGMENT always produces it regardless of
    --quantify_compartments, and it is recorded unconditionally: a later join against
    it (see subworkflows/local/postprocess.nf's `ch_mask`) is a plain, non-remainder
    join on exactly that invariant, and withholding it here would silently empty that
    join for every patient. row() rejects a MISSING KEY (the caller forgot a column)
    but accepts an EMPTY VALUE (the caller means "not produced"), and readers test for
    emptiness rather than for the column's absence. Keeping the schema fixed across
    param settings is what lets one header serve every run.
========================================================================================
*/

class Checkpoint {

    /*
     * The single table answering "what is in a checkpoint CSV?".
     *
     *   name    - the checkpoint's step name; the CSV basename Layout builds paths
     *             from, and a member of Layout.CHECKPOINT_STEPS.
     *   columns - the ordered column list. This IS the published contract with every
     *             reader (`--start`'s samplesheet parser, add_cycle's prior-run
     *             readers, tests/checkpoint_manifest.nf.test) — all of which now come
     *             through read() below. Changing an entry changes a published file's
     *             header.
     */
    static final List<Map> STEPS = [
        [
            name       : 'preprocessed',
            columns    : ['patient_id', 'preprocessed_image', 'is_reference', 'channels'].asImmutable(),
            imageColumn: 'preprocessed_image',
        ],
        [
            name       : 'registered',
            columns    : ['patient_id', 'registered_image', 'is_reference', 'channels'].asImmutable(),
            imageColumn: 'registered_image',
        ],
        [
            name       : 'segmented',
            columns    : ['patient_id', 'registered_image', 'is_reference', 'channels',
                          'cell_mask', 'nuclei_mask', 'contours', 'nucleus_contours'].asImmutable(),
            imageColumn: 'registered_image',
        ],
        [
            name       : 'postprocessed',
            columns    : ['patient_id', 'cell_csv', 'cell_geojson', 'merged_csv', 'cell_mask', 'pyramid'].asImmutable(),
            // No per-image column, deliberately: this checkpoint has ONE row per
            // patient (its artifacts are the patient's combined outputs, not a slide's),
            // so there is no image stem to derive an id from and `id` is the patient_id.
            // null is the null-object here, the same shape the adapters' optional emits
            // use -- read() branches on it rather than every caller special-casing this
            // step.
            imageColumn: null,
        ],
    ].asImmutable()

    private static Map requireStep(String step) {
        def entry = STEPS.find { it.name == step }
        if (!entry)
            throw new Checkpoint.UnknownStepException(
                "Checkpoint: unknown checkpoint step: '${step}'. Valid: ${STEPS*.name}")
        return entry
    }

    /**
     * Thrown when a caller names a step that is not in {@link #STEPS}. Its own class
     * (rather than a bare {@code IllegalArgumentException}) so a caller can tell, from
     * the exception type alone, that Checkpoint — not Layout, which throws the same
     * message text for the same reason — is the one that rejected the name.
     */
    static class UnknownStepException extends IllegalArgumentException {
        UnknownStepException(String message) { super(message) }
    }

    /**
     * The ordered column list for a checkpoint. The returned list is immutable
     * (`asImmutable()` is applied to each `columns:` entry in {@link #STEPS} above,
     * not just to the outer list) — mutating it must throw rather than silently
     * rewrite the schema every later `header()`/`row()` call in the run sees.
     */
    static List<String> columns(String step) {
        return requireStep(step).columns
    }

    /** The header line — the `seed:` value every writer passes to collectFile(). */
    static String header(String step) {
        return columns(step).join(',')
    }

    /**
     * The column holding the image a row is ABOUT, or null for a checkpoint whose rows
     * are per-patient rather than per-image (see 'postprocessed' in {@link #STEPS}).
     * This is what {@link #read} derives `meta.id` from.
     *
     * It is the same string ParamUtils.STEPS gives as the `entryColumn` of the step
     * that READS this checkpoint — two tables stating one fact, so tests/lib_probe.nf
     * asserts they agree rather than trusting them to.
     */
    static String imageColumn(String step) {
        return requireStep(step).imageColumn
    }

    /**
     * Read a checkpoint CSV: `[[meta, row], ...]`, one entry per data row.
     *
     * WHY THE READ SIDE BELONGS HERE. This class owned writing and nothing else, so
     * three callers wrote three readers of the files it produces: INPUT_CHECK
     * (subworkflows/local/input_check.nf), READ_SEGMENTED_CHECKPOINT
     * (subworkflows/local/segmentation.nf) and ADD_CYCLE's prior-run reader. They
     * disagreed on all three things a reader decides:
     *
     *   `id`           INPUT_CHECK built a per-image, patient-prefixed stem; the
     *                  segmented reader used row.patient_id, collapsing every one of a
     *                  patient's slides onto ONE id at --start postprocessing --
     *                  exactly the entry point where several patients share a collect
     *                  and identically named files overwrite each other;
     *   `is_reference` one strict parser (CsvUtils.parseIsReference) and three
     *                  structurally identical `value?.toLowerCase() == 'true'` copies,
     *                  so 'yes' raised at one entry point and became `false` at the
     *                  other two -- a checkpoint's reference row quietly ceasing to be
     *                  a reference;
     *   the counts     INPUT_CHECK injected images_count/channels_count; the other two
     *                  injected neither, so every per-patient groupTuple downstream of
     *                  --start postprocessing took its unsized fallback and streaming
     *                  was off for the whole run, unreported.
     *
     * THE META SHAPE IS THE SCHEMA'S, NOT THE CALLER'S. Every meta carries
     * `patient_id`, `id` and `images_count`; it additionally carries `is_reference`,
     * `channels` and `channels_count` exactly when the step DECLARES an `is_reference`
     * / `channels` column. A step with no `channels` column has no channel fan-out to
     * size, and inventing a count for it would be the same lie as omitting one where
     * the column exists. What is forbidden is a SILENT absence: where a count is
     * derivable it is derived, and a patient it cannot be derived for raises.
     *
     * EAGER, NOT A CHANNEL. It returns a plain List so it stays a pure static method
     * (callable from tests/lib_probe.nf, like everything else here) and so a malformed
     * checkpoint aborts at workflow-construction time with a message naming the file,
     * rather than as a null dereference inside some later operator. Callers wrap it in
     * `Channel.fromList(...)`. Checkpoints are one row per image, so reading one
     * eagerly costs nothing -- and the counts already required a whole-file read.
     *
     * @param step    a member of {@link #STEPS}
     * @param csvPath the checkpoint CSV to read
     * @param opts    `nuclear_markers` — params.nuclear_markers; REQUIRED when the step
     *                declares a `channels` column, since channels_count depends on which
     *                markers are nuclear and defaulting it here would be a second,
     *                silent declaration of a parameter nextflow.config already owns. It is
     *                never read raw: it goes to CsvUtils.countChannelsPerPatient and
     *                CsvUtils.parseMetadata, both of which funnel into
     *                MarkerUtils.markerList (tests/test_nuclear_marker_routing.py).
     *                It is the ONLY accepted key: {@link #OPTIONS} is closed and an
     *                unknown one throws, exactly as an unknown column does in row().
     *
     * THERE IS NO auto_reference OPTION, deliberately. An earlier draft carried
     * `opts.auto_reference ?: false`, which re-encoded a rule ParamUtils already owns
     * (autoReferenceAllowed) in a class no duplicate-default guard scans — and neither
     * caller ever passed it. It cannot be needed: auto-promotion applies at
     * `--start preprocessing` ONLY, and preprocessing's input is a user samplesheet,
     * never a checkpoint. So for everything this method can legitimately be handed the
     * answer is false by construction, and it is passed as the literal below rather
     * than as a second declaration of somebody else's rule.
     */
    /**
     * The complete set of keys {@link #read} accepts. Closed on purpose: a typo'd or
     * invented option that is silently ignored is how a caller comes to believe it
     * configured something it did not.
     */
    static final List<String> OPTIONS = ['nuclear_markers'].asImmutable()

    /**
     * A checkpoint always names its own reference, so a checkpoint reader never
     * auto-promotes one. Named rather than a bare `false` at two call sites so the
     * claim is greppable and points at its owner: ParamUtils.autoReferenceAllowed,
     * which returns true only at --start preprocessing -- an entry point whose input
     * is a samplesheet, and therefore never reaches this class.
     */
    private static final boolean NO_AUTO_REFERENCE = false

    static List<List> read(String step, def csvPath, Map opts = [:]) {
        def cols = columns(step)                      // throws UnknownStepException first

        def unknownOpts = (opts ?: [:]).keySet().findAll { !(it in OPTIONS) }
        if (unknownOpts)
            throw new IllegalArgumentException(
                "Checkpoint.read('${step}'): unknown option(s) ${unknownOpts as List}. " +
                "Valid options: ${OPTIONS}. (There is no auto_reference option — a " +
                "checkpoint always names its own reference; see ParamUtils.autoReferenceAllowed.)")
        def path = csvPath?.toString()
        if (!path?.trim())
            throw new IllegalArgumentException(
                "Checkpoint.read('${step}'): no CSV path given (got ${csvPath == null ? 'null' : "'${csvPath}'"})")
        if (!new File(path).exists())
            throw new FileNotFoundException("Checkpoint.read('${step}'): no such checkpoint CSV: ${path}")

        // The column check the three readers each hand-rolled. Theirs asserted that the
        // columns THEY index are ones Checkpoint still declares (schema drift in one
        // direction); this additionally asserts the FILE really has them (drift in the
        // other), which is what a reader actually depends on.
        def header  = CsvUtils.readHeader(path)
        def missing = cols.findAll { !(it in header) }
        if (missing)
            throw new IllegalArgumentException(
                "Checkpoint.read('${step}'): ${path} is missing column(s) ${missing}. " +
                "A '${step}' checkpoint declares, in order: ${cols}. Header found: ${header}")

        def rows = CsvUtils.readRows(path)
        if (!rows)
            throw new IllegalStateException(
                "Checkpoint.read('${step}'): ${path} has a header but no data rows")

        def hasChannels = 'channels' in cols
        def hasRefCol   = 'is_reference' in cols
        def imageCol    = imageColumn(step)
        def nuclear     = opts.nuclear_markers
        if (hasChannels && nuclear == null)
            throw new IllegalArgumentException(
                "Checkpoint.read('${step}'): nuclear_markers is required for a step that " +
                "declares a 'channels' column — meta.channels_count is the count of markers " +
                "that survive the nuclear-channel drop, which cannot be derived without it. " +
                "Pass the pipeline's nuclear_markers parameter as opts.nuclear_markers.")

        def imageCounts   = CsvUtils.countImagesPerPatient(path)
        def channelCounts = hasChannels
            ? CsvUtils.countChannelsPerPatient(path, imageCol, nuclear, NO_AUTO_REFERENCE)
            : [:]
        // THE reference decision, made once per file, from the file — the same call
        // INPUT_CHECK makes and the same call countChannelsPerPatient makes internally,
        // so the reference that SIZED channels_count and the reference stamped into meta
        // are the same row by construction rather than by convention.
        def referenceImage = hasRefCol && imageCol
            ? CsvUtils.resolveReferenceRows(path, imageCol, NO_AUTO_REFERENCE)
            : [:]

        return rows.withIndex().collect { row, i ->
            def ctx = "row ${i + 2} of ${path}"
            def pid = row.patient_id?.toString()?.trim()
            if (!pid)
                throw new IllegalArgumentException("Checkpoint.read('${step}'): blank patient_id in ${ctx}")

            // parseMetadata is INPUT_CHECK's own base meta (patient_id + strict
            // is_reference + channels, validated for a nuclear marker). Calling it rather
            // than rebuilding those three keys is what makes "same meta shape" structural.
            def meta = hasRefCol && hasChannels
                ? CsvUtils.parseMetadata(row, nuclear, ctx)
                : [patient_id: pid]
            if (hasRefCol && imageCol)
                meta.is_reference = referenceImage[pid] != null &&
                                    referenceImage[pid] == row[imageCol]?.toString()?.trim()

            if (imageCol) {
                def image = row[imageCol]?.toString()?.trim()
                if (!image)
                    throw new IllegalArgumentException(
                        "Checkpoint.read('${step}'): empty '${imageCol}' in ${ctx}. A checkpoint " +
                        "row's image path is what every downstream file name is derived from.")
                meta.id = CsvUtils.imageId(pid, image)
            }
            else {
                meta.id = pid
            }

            meta.images_count = requireCount(imageCounts, pid, 'images_count', step, path)
            if (hasChannels)
                meta.channels_count = requireCount(channelCounts, pid, 'channels_count', step, path)

            return [meta, row]
        }
    }

    /**
     * A count that is present and positive, or a named error. A count of null or 0 is
     * what silently disables streaming for a whole run: every sized groupTuple falls
     * back to "wait for the channel to close", the run still exits 0, and nothing says
     * so. That is the failure this method exists to make loud.
     */
    private static Integer requireCount(Map counts, String pid, String key, String step, String path) {
        def n = counts[pid]
        if (!(n instanceof Integer) || n < 1)
            throw new IllegalStateException(
                "Checkpoint.read('${step}'): cannot derive ${key} for patient '${pid}' from ${path} " +
                "(got ${n}). Every meta must carry both counts: they size the per-patient " +
                "groupTuples, and an absent one silently degrades them to unsized for the " +
                "whole run rather than failing.")
        return n
    }

    /**
     * One CSV row, values ordered by the DECLARED column list.
     *
     * Throws on a missing key (an empty field is a path that does not resolve) and
     * on an unknown key (the caller's idea of the schema disagrees with this one).
     * Does NOT validate the VALUES: a key present with a `null` value passes through
     * and is joined as the literal text `null`, exactly as the hand-written GStrings
     * this class replaced did. Only key presence is a contract here.
     */
    /**
     * The full TEXT of a per-patient checkpoint fragment: the step's header, then the
     * rows, newline-terminated.
     *
     * ONE SCHEMA, NOT TWO. A fragment (`<outdir>/csv/<step>.parts/<pid>.csv`, see
     * lib/Layout.groovy) and the aggregate (`<outdir>/csv/<step>.csv`) are the same
     * checkpoint at two granularities, so they must be the same FILE FORMAT: a reader
     * that can open one opens the other. Both get their header from {@link #header}
     * and their rows from {@link #row}; this method only decides that the header comes
     * first and the lines are separated by newlines — exactly what collectFile's
     * `seed:` + `newLine: true` do for the aggregate.
     *
     * IT IS ALSO WHY modules/local/write_checkpoint_fragment.nf's `script:` and `stub:`
     * blocks cannot diverge. `-stub` never evaluates a `script:` block, so a stub block
     * that built the file its own way would be the only version CI's blocking gate ever
     * runs, and the real one could rot unseen. Both blocks call this. Same reasoning as
     * lib/ProcessEnvelope.groovy's versions()/versionsStub() pair.
     *
     * The trailing newline is deliberate: a POSIX text file ends in one, and
     * collectFile's `newLine: true` gives the aggregate the same.
     */
    static String fragment(String step, List<String> rows) {
        return ([header(step)] + (rows ?: [])).join('\n') + '\n'
    }

    static String row(String step, Map values) {
        def cols    = columns(step)
        def missing = cols.findAll { !values.containsKey(it) }
        if (missing)
            throw new IllegalArgumentException(
                "Checkpoint.row('${step}'): missing column(s) ${missing}. " +
                "Required, in order: ${cols}")

        def unknown = values.keySet().findAll { !(it in cols) }
        if (unknown)
            throw new IllegalArgumentException(
                "Checkpoint.row('${step}'): unknown column(s) ${unknown as List}. " +
                "Valid columns: ${cols}")

        return cols.collect { values[it] }.join(',')
    }
}
