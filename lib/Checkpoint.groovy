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
    presence only, not value shape: a key present with an empty-string VALUE passes
    through unexamined (that is the deliberate "not produced" signal, see EMPTY
    VALUES below), and a key present with a `null` VALUE is written as an empty
    field too (see `row()`'s own doc — it used to be joined as the literal four-
    character text `null`, which read back as a bogus non-empty path/id).

    SCOPE. Like Layout, this class never reads `params`: every method takes what it
    needs as an argument, so it is callable from an onComplete handler and from
    tests/lib_probe.nf. All methods are static.

    RFC 4180 QUOTING, IN ONE PLACE. `row()` quotes a value that contains a comma, a
    double quote, or a newline (doubling any embedded `"`, per RFC 4180) and leaves
    every other value bare. This is not defensive for its own sake: `--outdir` is an
    arbitrary filesystem path and every published path is built from it, so a comma
    anywhere in `--outdir` used to shift every later column of every row that
    embedded it -- silently, since a bare `.join(',')` cannot tell "a value with a
    comma in it" from "a column boundary". `channels` still uses `|` as its own
    internal separator (so a channel *name* containing a comma does not itself force
    quoting of that field), but the field-level join below no longer assumes nothing
    else ever will. Every reader parses with Nextflow's `splitCsv(header: true)`,
    which understands RFC 4180 quoting natively -- writing it here needed no reader
    change.

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

    RULING R17 (identity is carried, never re-derived). Every STEPS entry carries an
    `id` column, right after `patient_id`. The file a checkpoint names is a DERIVED
    artifact whose basename differs from whatever produced it (`preprocessed_image`
    is e.g. `P001_slide_corrected.ome.tiff`; a `postprocessed` row names a pyramid) --
    deriving a stem from that basename cannot reproduce the identity a samplesheet row
    was originally assigned, and would manufacture a DIFFERENT identity depending on
    which checkpoint happened to be the entry point. `id` closes that: a checkpoint
    reader (lib/Meta.groovy's `fromCheckpointRow`) reads the value back rather than
    re-deriving it. A checkpoint written before this column existed has no `id` field
    in its row Map at all (`splitCsv(header:true)` only creates keys for columns the
    FILE's own header declares) -- `fromCheckpointRow` detects exactly that shape and
    fails with a message naming the fix (re-run the step that wrote the file), rather
    than silently falling back to a re-derived, possibly-different id.

    SCALE IS CARRIED THE SAME WAY. Every STEPS entry now also ends in a `pixel_size`
    column: the micrometres-per-pixel `params.pixel_size == 'auto'` resolves to for
    that run (subworkflows/local/input_check.nf's PREFLIGHT_SCALE), threaded into
    meta and recorded here so a completed run's checkpoint says what scale it was
    processed at, rather than making `--start segmentation`/`--start postprocessing`
    -- which build meta ENTIRELY from this CSV -- re-derive or re-guess it. Same
    "throw on an old file, never fall back to params.pixel_size" contract as `id`:
    see lib/Meta.groovy's `fromCheckpointRow`. Appended last on every schema, not
    inserted, so this column's addition does not renumber any existing one.
========================================================================================
*/

class Checkpoint {

    /*
     * The single table answering "what is in a checkpoint CSV?".
     *
     *   name    - the checkpoint's step name; the CSV basename Layout builds paths
     *             from, and a member of Layout.CHECKPOINT_STEPS.
     *   columns - the ordered column list. This IS the published contract with every
     *             reader (`--start`'s samplesheet parser, add_cycle's splitCsv,
     *             tests/checkpoint_manifest.nf.test). Changing an entry changes a
     *             published file's header.
     */
    static final List<Map> STEPS = [
        [
            name   : 'preprocessed',
            columns: ['patient_id', 'id', 'preprocessed_image', 'is_reference', 'channels', 'pixel_size'].asImmutable(),
        ],
        [
            name   : 'registered',
            columns: ['patient_id', 'id', 'registered_image', 'is_reference', 'channels', 'pixel_size'].asImmutable(),
        ],
        [
            name   : 'segmented',
            columns: ['patient_id', 'id', 'registered_image', 'is_reference', 'channels',
                      'cell_mask', 'nuclei_mask', 'contours', 'nucleus_contours', 'pixel_size'].asImmutable(),
        ],
        [
            // 'postprocessed' rows are per-PATIENT, not per-slide (there is no single
            // "the" slide a pyramid/merged table belongs to) -- its writer records `id:
            // patient_id`, the same synthetic patient-level id add_cycle.nf already uses
            // for its own patient-scoped metas ([patient_id: pid, id: pid, ...]).
            name   : 'postprocessed',
            columns: ['patient_id', 'id', 'cell_csv', 'cell_geojson', 'merged_csv', 'cell_mask', 'pyramid', 'pixel_size'].asImmutable(),
        ],
    ].asImmutable()

    /**
     * Whether a checkpoint CSV is written for `step` at `level`.
     *
     * A checkpoint's whole purpose is to name WHERE a step's artifacts landed, so a
     * later run can re-enter there. When --cleanup_level stops publishing those
     * artifacts, the honest thing is to write no manifest at all rather than one
     * whose rows name files that were never created.
     * tests/checkpoint_manifest.nf.test asserts that every recorded path resolves,
     * and that assertion is load-bearing: a manifest whose paths dangle is worse
     * than no manifest, because `--start` and add_cycle both open what it names.
     *
     * NO step qualifies at a cleaning level, INCLUDING 'postprocessed' -- which is
     * not obvious, and is the reason this returns a flat `level == 'none'` rather
     * than a per-step table. 'postprocessed' looks safe: cell_csv, cell_geojson,
     * merged_csv and pyramid are all in Layout.FINAL_KINDS. But its `cell_mask`
     * column names the segmentation mask, and 'segmentation' is an intermediate,
     * so that one column dangles. Confirmed by running the manifest test against
     * the gated config, which reported exactly:
     *
     *     postprocessed.csv: does not exist -> .../P001/segmentation/P001_cell_mask.tif
     *
     * Adding 'segmentation' to Layout.FINAL_KINDS would make the postprocessed
     * manifest writable again. That is a deliberate NON-choice: the surviving set
     * is the user's, and masks are reconstructible from the pyramid when
     * embed_masks is on. Revisit here, not by special-casing a column.
     */
    static boolean writesAtLevel(String step, String level) {
        requireStep(step)
        return level == 'none'
    }

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

    /**
     * Assert that `step` still declares every column in `cols`.
     *
     * WHAT THIS REPLACES. Three readers each carried the same seven-line block —
     * `['patient_id', 'registered_image', ...].each { col -> if (!(col in
     * Checkpoint.columns(...))) throw new IllegalStateException(...) }` —
     * subworkflows/local/add_cycle.nf twice (its registered and postprocessed reads) and
     * subworkflows/local/segmentation.nf once (READ_SEGMENTED_CHECKPOINT). Three copies
     * of one rule, each free to word its failure differently, and each an independent
     * opportunity to fall behind a schema change.
     *
     * WHY THE CHECK EXISTS AT ALL, which is easy to lose when it becomes a one-liner.
     * A reader indexes a checkpoint row by column NAME through
     * `splitCsv(header: true)`, which creates keys only for columns the FILE's header
     * declares. So a reader naming a column the writer stopped emitting does not fail —
     * it reads `null`, which becomes an empty field, which becomes a path that does not
     * resolve, several steps later and a long way from the cause. Failing at workflow
     * construction, naming the column, is the whole point.
     *
     * Reports EVERY missing column, not just the first: a reader that lost two learns
     * both in one run rather than one per edit-and-rerun cycle.
     *
     * An empty `cols` throws rather than passing. A caller that checks nothing reads as
     * covered, which is the shape of guard this repo has been bitten by repeatedly.
     *
     * Throws {@link UnknownStepException} for an unknown step (via {@link #columns}) and
     * {@code IllegalStateException} for a declared step missing a requested column — two
     * types, because "this step never existed" and "this schema changed under me" are
     * different problems with different fixes.
     */
    static void requireColumns(String step, Collection<String> cols) {
        def declared = columns(step)
        if (!cols)
            throw new IllegalArgumentException(
                "Checkpoint.requireColumns('${step}'): no columns requested. A caller " +
                "that checks nothing reads as covered; name the columns it indexes.")

        def missing = cols.findAll { !(it in declared) }
        if (missing)
            throw new IllegalStateException(
                "Checkpoint.requireColumns('${step}'): column(s) ${missing as List} are " +
                "read from the '${step}' checkpoint but Checkpoint no longer declares " +
                "them. Declared, in order: ${declared}. A reader indexing an undeclared " +
                "column gets an empty field, i.e. a path that does not resolve, several " +
                "steps later.")
    }

    /** The header line — the `seed:` value every writer passes to collectFile(). */
    static String header(String step) {
        return columns(step).join(',')
    }

    /**
     * One CSV row, values ordered by the DECLARED column list.
     *
     * Throws on a missing key (an empty field is a path that does not resolve) and
     * on an unknown key (the caller's idea of the schema disagrees with this one).
     * Does NOT validate the VALUES for key presence beyond that -- but a `null`
     * value is now written as an empty field, exactly like the empty string a
     * "this artifact was not produced" caller already passes (see the EMPTY VALUES
     * note above): both mean "nothing here", and readers already treat an empty
     * field as absence (e.g. `Meta.fromCheckpointRow`'s `requirePresentInRow`
     * rejects a blank value for a column it requires). Writing `null` through as
     * the four-character text `null` -- the previous behaviour -- made that text
     * indistinguishable from a real four-character path/id on read-back; nothing
     * upstream is known to pass `null` today (every writer already uses `''` for
     * "not produced"), but a caller that changes should get an absent field, not a
     * corrupt one.
     *
     * A value is RFC 4180-quoted (wrapped in `"..."`, with any embedded `"`
     * doubled) when it contains a comma, a double quote, or a newline. This is
     * what stops a comma inside `--outdir` -- present in every published path --
     * from silently shifting every later column.
     */
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

        return cols.collect { col -> quoteField(values[col]) }.join(',')
    }

    /**
     * One field, RFC 4180-encoded. `null` becomes the empty field (see `row()`'s
     * doc); any other value is quoted only when it needs to be, so an ordinary
     * path/id/boolean is unchanged from the bare-join output this replaces.
     */
    private static String quoteField(def value) {
        if (value == null) return ''
        def s = value.toString()
        return (s.contains(',') || s.contains('"') || s.contains('\n'))
            ? '"' + s.replace('"', '""') + '"'
            : s
    }
}
