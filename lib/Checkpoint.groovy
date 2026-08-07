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
            columns: ['patient_id', 'preprocessed_image', 'is_reference', 'channels'].asImmutable(),
        ],
        [
            name   : 'registered',
            columns: ['patient_id', 'registered_image', 'is_reference', 'channels'].asImmutable(),
        ],
        [
            name   : 'postprocessed',
            columns: ['patient_id', 'cell_csv', 'cell_geojson', 'merged_csv', 'cell_mask', 'pyramid'].asImmutable(),
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
     * One CSV row, values ordered by the DECLARED column list.
     *
     * Throws on a missing key (an empty field is a path that does not resolve) and
     * on an unknown key (the caller's idea of the schema disagrees with this one).
     * Does NOT validate the VALUES: a key present with a `null` value passes through
     * and is joined as the literal text `null`, exactly as the hand-written GStrings
     * this class replaced did. Only key presence is a contract here.
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

        return cols.collect { values[it] }.join(',')
    }
}
