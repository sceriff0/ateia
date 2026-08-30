/*
========================================================================================
    Meta — the ONE constructor for every meta map
========================================================================================
    Why this exists: INPUT_CHECK (subworkflows/local/input_check.nf) and the
    checkpoint readers (e.g. subworkflows/local/segmentation.nf's
    READ_SEGMENTED_CHECKPOINT-equivalent) used to build `[patient_id: ..., id: ...,
    is_reference: ..., channels: ...]` maps independently. The checkpoint reader
    omitted `keep_channels` and `channels_count`, so `--start postprocessing`
    silently diverged from a full run: SPLIT_CHANNELS emitted an extra DAPI plane,
    QUANTIFY ran twice, and postprocess.nf's `.unique()` kept whichever copy
    ARRIVED first -- so the pyramid's DAPI plane came from a nondeterministic slide.

    With one constructor, every entry point produces the same key set by
    construction, and `--start X` is verifiably equivalent to a full run from that
    point on.

    SCOPE. Like Layout and Checkpoint, this class never reads `params`: every
    method takes what it needs as an argument. All methods are static (project
    convention for lib/ -- see CLAUDE.md).

    STATUS (as of Task 4.3). Both producers named above are converted:
    subworkflows/local/input_check.nf (Task 4.2) and
    subworkflows/local/segmentation.nf's READ_SEGMENTED_CHECKPOINT (Task 4.3, which
    also added the `id` column every Checkpoint.STEPS schema needed for
    `fromCheckpointRow` to be callable at all -- see RULING R17 in
    lib/Checkpoint.groovy). tests/test_meta_module.py's
    `test_only_meta_groovy_constructs_a_meta_map` guard is no longer xfail'd; it
    passes for real. The one deliberate, documented exception is
    subworkflows/local/adapters/valis_adapter.nf's `[patient_id: patient_id]` --
    not a per-image sample meta at all (it carries none of REQUIRED_KEYS and never
    reaches an `emit:`), but a REGISTER-process control tuple; see that guard's
    `ALLOWED` comment for the full reasoning.
========================================================================================
*/

class Meta {

    /**
     * Every key a meta map must carry, at every entry point. `requireComplete`
     * enforces this on every map this class returns -- a missing key throws
     * rather than silently defaulting, because a silent default is what made the
     * two producers' divergence invisible for so long.
     */
    static final List<String> REQUIRED_KEYS = [
        'patient_id', 'id', 'is_reference', 'channels',
        'keep_channels', 'channels_count', 'images_count',
    ].asImmutable()

    /**
     * Build a meta from a samplesheet row.
     *
     * @param row         the parsed CSV row (a Map; `row[imageColumn]` is this
     *                    step's entry image, e.g. what column
     *                    CsvUtils.parseCsvLine/splitCsv produced)
     * @param imageColumn which column holds this step's entry image
     * @param rowIndex    0-based index of this row within its patient's rows, in
     *                    samplesheet order. Used ONLY to disambiguate ids that
     *                    would otherwise collide -- see identityFor.
     * @param ctx         [keepChannelsBySlide: Map, imagesCount: Map,
     *                     channelsCount: Map, stemCounts: Map] -- pre-computed
     *                     once by the caller, exactly as
     *                     CsvUtils.countImagesPerPatient is today. stemCounts is
     *                     OPTIONAL: a "patientId::stem" -> row-count map used by
     *                     identityFor to detect a collision; absent, every stem
     *                     is treated as unique (n=1, no suffix).
     */
    static Map fromSamplesheetRow(Map row, String imageColumn, int rowIndex, Map ctx) {
        requirePresentInRow(row, 'patient_id')
        requirePresentInRow(row, imageColumn)

        def patientId = row.patient_id.toString().trim()
        def rawImage  = row[imageColumn].toString().trim()

        def meta = [
            patient_id  : patientId,
            id          : identityFor(patientId, rawImage, rowIndex, ctx),
            is_reference: row.is_reference?.toString()?.toLowerCase() == 'true',
            channels    : splitChannels(row.channels),
        ]
        // slideKey is meta.id, not the raw image cell: ctx.keepChannelsBySlide is now
        // CsvUtils.resolveKeptChannelsPerSlide's map, which keys its inner map on the
        // SAME identityFor output (see that method's doc for why) -- so this must look
        // up by the identity it just assigned, exactly as fromCheckpointRow below
        // already does with meta.id. Looking up by the raw rawImage cell instead (a
        // key that plainly isn't in an identity-keyed map at all) would miss cleanly
        // and fall through to meta.channels via finish()'s ABSENT-vs-EMPTY branch --
        // wrong, but loud in principle, since every row would get the full declared
        // list rather than a real per-slide answer.
        //
        // THE WORSE FAILURE IS NOT THAT ONE. It is `rowIndex` itself being wrong for a
        // colliding row (this task's Critical fix round: CsvUtils.rowIndexPerPatient
        // used to collapse two rows sharing a raw cell to the SAME index, so
        // identityFor assigned them the SAME meta.id). That does not miss the lookup
        // at all -- the map DOES contain an entry under that id, just the WRONG row's.
        // A row can therefore silently inherit another row's keep_channels (verified:
        // the reference row read back the second row's [], not its own [DAPI, CD3]),
        // which finish()'s containsKey-based ABSENT-vs-EMPTY check cannot catch, because
        // the entry is genuinely present -- just for the wrong slide. Correctness here
        // depends on identityFor's inputs (patientId, rawImage, rowIndex, stemCounts)
        // being the SAME values resolveKeptChannelsPerSlide computed its key from, not
        // merely on which field name finish() is called with.
        return finish(meta, meta.id, ctx)
    }

    /**
     * Build a meta from a checkpoint row (a row of `csv/<step>.csv`, read back in
     * at a `--start` entry point). Produces the SAME key set as
     * fromSamplesheetRow -- that equivalence is the whole point of this class.
     *
     * @param row  the checkpoint CSV row, already split on the checkpoint's
     *             column list (see lib/Checkpoint.groovy, which owns that list)
     * @param step which checkpoint this row came from, e.g. `'preprocessed'` --
     *             one of lib/Checkpoint.groovy's STEPS names. Validated against
     *             THAT schema (read from Checkpoint, never restated here): a row
     *             claiming to be from a step whose schema doesn't carry
     *             `is_reference`/`channels` must not silently produce a
     *             plausible-looking `is_reference=false` / `channels=[]` instead
     *             of an error.
     * @param ctx  same shape as fromSamplesheetRow's ctx.
     */
    static Map fromCheckpointRow(Map row, String step, Map ctx) {
        if (!step?.toString()?.trim())
            throw new IllegalArgumentException("Meta.fromCheckpointRow: 'step' must not be blank")

        // The single owner of "what is in a checkpoint CSV?" is lib/Checkpoint.groovy
        // (Checkpoint.STEPS). Reading the column list from there -- rather than a
        // second hardcoded list here -- is what stops this method's idea of the
        // schema from drifting away from Checkpoint's. An unknown step name throws
        // Checkpoint.UnknownStepException, which extends IllegalArgumentException.
        def schemaColumns = Checkpoint.columns(step)

        requirePresentInRow(row, 'patient_id')

        // Validate against the SCHEMA THIS STEP ACTUALLY DECLARES TODAY (3 of the 4
        // Checkpoint.STEPS carry is_reference/channels; 'postprocessed' carries
        // neither). Checked BEFORE the id gate below on purpose: these two are
        // per-row problems with the caller's data (fixable by the caller, on this
        // row), whereas the id gate is a systemic schema gap no caller can fix by
        // editing a row -- surfacing the specific, actionable error first is more
        // useful than burying it behind the universal one.
        if (schemaColumns.contains('is_reference'))
            requirePresentInRow(row, 'is_reference')
        if (schemaColumns.contains('channels'))
            requirePresentInRow(row, 'channels')

        // RULING R17, landed: every Checkpoint.STEPS schema now DECLARES an `id`
        // column (schemaColumns.contains('id') is therefore always true from here
        // on -- checked against Checkpoint's own table, never restated, in case a
        // future step is ever added without one). What still needs catching is a
        // REAL checkpoint FILE written before this column existed: `splitCsv(header:
        // true)` only creates a Map key for a column the file's own header line
        // actually declares, so an old file's row simply has no 'id' key at all --
        // `row.containsKey('id')` is what tells that apart from "the column exists
        // but this one row's value is blank" (a malformed/hand-edited file, which
        // requirePresentInRow below catches with its own, more generic message).
        // This is a per-FILE check (the row shape), not a per-STEP check (the code's
        // current schema) -- schemaColumns.contains('id') can never again be false,
        // so testing that here would never fire for the real migration case this
        // exists to catch.
        if (!schemaColumns.contains('id'))
            throw new IllegalStateException(
                "Meta.fromCheckpointRow('${step}'): lib/Checkpoint.groovy's '${step}' schema " +
                "does not declare an 'id' column. Every STEPS entry must carry one (RULING R17) " +
                "-- this is a Checkpoint.groovy bug, not a bad checkpoint file.")
        if (!row.containsKey('id'))
            throw new IllegalStateException(
                "Meta.fromCheckpointRow('${step}'): this '${step}' checkpoint row has no 'id' " +
                "column. This checkpoint predates identity tracking (RULING R17) -- re-run the " +
                "step that WROTE this checkpoint so the regenerated file records identity, " +
                "rather than forcing it to be re-derived from a filename.")
        requirePresentInRow(row, 'id')

        // Same shape of gap as `id` above, one column later: every Checkpoint.STEPS
        // schema now declares a `pixel_size` column, so schemaColumns.contains(...)
        // can never again be false from here on -- checked against Checkpoint's own
        // table anyway, in case a future step is ever added without one. What still
        // needs catching is a REAL checkpoint FILE written before this column
        // existed: `splitCsv(header: true)` only creates a Map key for a column the
        // file's own header line actually declares, so an old file's row simply has
        // no 'pixel_size' key at all. Do NOT fall back to params.pixel_size here: on
        // a --start path that value may still be the literal 'auto' and no image is
        // being read, so a fallback would reinstate the exact null-scale failure
        // this column exists to close.
        if (!schemaColumns.contains('pixel_size'))
            throw new IllegalStateException(
                "Meta.fromCheckpointRow('${step}'): lib/Checkpoint.groovy's '${step}' schema does " +
                "not declare a 'pixel_size' column. Every STEPS entry must carry one -- this is a " +
                "Checkpoint.groovy bug, not a bad checkpoint file.")
        if (!row.containsKey('pixel_size'))
            throw new IllegalStateException(
                "Meta.fromCheckpointRow('${step}'): this '${step}' checkpoint row has no " +
                "'pixel_size' column. This checkpoint predates scale tracking -- re-run the step " +
                "that WROTE it so the regenerated manifest records the resolved micrometres-per-" +
                "pixel, rather than leaving every downstream measurement to be scaled by a value " +
                "nothing recorded.")
        requirePresentInRow(row, 'pixel_size')

        def meta = [
            patient_id  : row.patient_id.toString().trim(),
            // A checkpoint row carries the id ASSIGNED at samplesheet-read time.
            // Never re-derive it from a filename here: that is precisely how the
            // two producers drifted apart.
            id          : row.id.toString().trim(),
            is_reference: row.is_reference?.toString()?.toLowerCase() == 'true',
            channels    : splitChannels(row.channels),
            pixel_size  : row.pixel_size as Double,
        ]
        return finish(meta, meta.id, ctx)
    }

    /**
     * Identity, ASSIGNED at read time -- never derived from a basename alone.
     *
     * meta.id used to be `file(row[imageColumn]).simpleName`, which collides
     * across the ordinary cyclic-IF layout (cycle1/slide.ome.tiff and
     * cycle2/slide.ome.tiff both give "slide"). Both rows then recorded the same
     * path in preprocessed.csv, and the run aborted AFTER the corrupt manifest
     * was written.
     *
     * The stem is kept when it is unique within the patient -- so existing output
     * filenames are unchanged for every non-colliding sheet -- and disambiguated
     * with the row index only where it would otherwise collide. Given a fixed
     * samplesheet the result is deterministic, which resume caching requires.
     *
     * RULING R2, VERIFIED: `file(...).simpleName` strips EVERY extension, not
     * just one -- confirmed by direct execution against this repo's pinned
     * Nextflow (26.04.6): `file('slide.ome.tiff').simpleName == 'slide'`, and
     * `file('a.b.c.tiff').simpleName == 'a'`. (`.baseName`, not `.simpleName`, is
     * the one that strips only the last extension: 'slide.ome.tiff' -> 'slide.ome'.)
     * So reproducing `.simpleName` for a non-colliding row -- the byte-identical
     * published-manifest requirement Task 4.2 Step 5 checks -- means stripping
     * from the FIRST '.' onward, not the last. Stripping only the last extension
     * would leave `.ome` on every id derived from a `*.ome.tiff` input even where
     * nothing collides, which is exactly the silent migration this ruling forbids.
     */
    static String identityFor(String patientId, String rawImage, int rowIndex, Map ctx) {
        def name = new File(rawImage.toString()).name
        def dot  = name.indexOf('.')
        def stem = dot >= 0 ? name.substring(0, dot) : name
        def base = stem.startsWith(patientId) ? stem : "${patientId}_${stem}"

        def counts = (ctx?.stemCounts ?: [:]) as Map
        def key    = "${patientId}::${stem}".toString()
        def n      = (counts[key] ?: 1) as int
        return n > 1 ? "${base}_${String.format('%03d', rowIndex)}" : base
    }

    // ----------------------------------------------------------------- private

    private static Map finish(Map meta, def slideKey, Map ctx) {
        // keep_channels is keyed on IDENTITY (slideKey), not on the raw row map.
        // Keying it on the row itself meant a patient's second pass overwrote the
        // first with [], and channels_count then summed to zero.
        def perPatient = (ctx?.keepChannelsBySlide ?: [:] ) as Map
        def perSlide   = perPatient[meta.patient_id]
        // ABSENT vs EMPTY, and never `?:`. Groovy treats [] as falsy, so `?:`
        // cannot tell "this slide emits nothing" (legitimate -- every channel it
        // declares was already claimed by an earlier slide of this patient) from
        // "no entry for this slide" (fall back to the declared list).
        meta.keep_channels = (perSlide instanceof Map && perSlide.containsKey(slideKey))
            ? perSlide[slideKey]
            : meta.channels

        // The dual invariant: channels_count must equal BOTH the number of
        // distinct channel names AND the number of emitted files. One consumer
        // .unique()s the list and one does not, so any divergence desynchronises
        // a streaming groupTuple(size:) and the run hangs or mis-groups.
        //
        // This is a per-PATIENT total (CsvUtils.countChannelsPerPatient sums
        // keep_channels sizes ACROSS a patient's slides), so it cannot be derived
        // from this one row/slide alone -- it can only come from ctx.channelsCount,
        // which every caller is required to pre-compute up front for the WHOLE
        // sheet/checkpoint before calling Meta (exactly as it already must for
        // ctx.imagesCount, below). There is deliberately NO fallback to
        // meta.keep_channels.size() (a per-SLIDE count) and no opt-out for a
        // caller with "nothing to give": channels_count feeds groupKey(patient_id,
        // channels_count) (subworkflows/local/quantify_markers.nf), and a count
        // that is too low does not raise there -- it makes the group emit early
        // with missing members, or never emit and hang, far from this call site.
        // A caller that cannot supply a real per-patient count has a bug to fix
        // (compute one), not a case this method should paper over.
        meta.channels_count = requirePerPatientCount(ctx, 'channelsCount', meta.patient_id,
            'CsvUtils.countChannelsPerPatient is the samplesheet-path example')

        // images_count is the SAME shape of obligation as channels_count just
        // above, and it is equally load-bearing: it feeds
        // groupKey(meta.patient_id, meta.images_count) in
        // subworkflows/local/registration.nf. This used to be
        // `(ctx?.imagesCount ?: [:])[patientId] ?: 1` -- worse than
        // channels_count's old bug, because Groovy's `?:` treats an explicit `0`
        // as falsy too, so a genuine `images_count: 0` would have been silently
        // coerced to `1` instead of merely falling back to a differently-wrong
        // number. Same fix, same reasoning, same helper.
        meta.images_count = requirePerPatientCount(ctx, 'imagesCount', meta.patient_id,
            'CsvUtils.countImagesPerPatient')

        requireComplete(meta)
        return meta
    }

    /**
     * ctx[ctxKey] must carry a per-patient entry -- see finish()'s comments on
     * channels_count/images_count for why there is no fallback for either. Uses
     * containsKey, not a truthy/`?:` check, so a patient that GENUINELY has a
     * count of 0 (e.g. every declared channel already claimed elsewhere) is
     * distinguished from a patient with no entry at all -- the same ABSENT-vs-EMPTY
     * distinction keep_channels above already has to make. One helper, not one
     * per field, so channels_count and images_count cannot drift into two
     * different idioms for the identical rule.
     *
     * @param ctx           the caller's context map
     * @param ctxKey        which per-patient map to read, e.g. 'channelsCount'
     * @param patientId     the patient this meta is being built for
     * @param resolverHint  named in the exception message: where a caller should
     *                      get this count from
     */
    private static int requirePerPatientCount(Map ctx, String ctxKey, String patientId, String resolverHint) {
        def counts = ctx?.get(ctxKey)
        if (!(counts instanceof Map) || !counts.containsKey(patientId))
            throw new IllegalArgumentException(
                "Meta: ctx.${ctxKey} has no entry for patient '${patientId}'. Every caller must " +
                "pre-compute a per-patient ${ctxKey} (${resolverHint}) covering every patient " +
                "before calling Meta. There is no fallback: a silently-wrong count desynchronises " +
                "a streaming groupTuple(size:)/groupKey downstream, which hangs or mis-groups far " +
                "from here.")
        return counts[patientId] as int
    }

    private static List<String> splitChannels(def raw) {
        return (raw ?: '').toString().split('\\|').collect { it.trim() }.findAll { it }
    }

    private static void requirePresentInRow(Map row, String col) {
        if (row == null || !row.containsKey(col) || row[col] == null || row[col].toString().trim() == '')
            throw new IllegalArgumentException(
                "Meta: required column '${col}' is missing or empty. Row: ${row}")
    }

    private static void requireComplete(Map meta) {
        // Fail loudly on a missing key rather than defaulting. A silent default
        // is what made the two producers' divergence invisible for so long.
        def missing = REQUIRED_KEYS.findAll { !meta.containsKey(it) }
        if (missing)
            throw new IllegalStateException(
                "Meta is incomplete -- missing ${missing}. Every entry point must " +
                "produce the same key set, or --start X diverges from a full run.")
    }
}
