/*
================================================================================
    SUBWORKFLOW: INPUT_CHECK
================================================================================
    Samplesheet in, sample channel out.

    This is the only place that turns a CSV row into a `[meta, file]` tuple, and
    the only place that pre-computes the per-patient image and channel totals.
    Those two jobs belong together: the counts exist solely to be injected into
    each meta as `images_count` / `channels_count`, which is what lets every
    downstream `groupTuple(by:, size:)` stream instead of waiting for the whole
    channel. Splitting them (counts in the router, injection in a helper) is what
    let the two drift apart before.

    All three linear entry points (`--start preprocessing|registration|
    postprocessing`) and the add_cycle path use this, differing only in which
    column holds the image to carry forward and in the `auto_reference` flag.

    NOTE on the `take:` values: Nextflow binds workflow inputs verbatim, so
    `samplesheet`, `image_column` and `auto_reference` arrive as the plain
    String/boolean the caller passed — not as channels. Only `emit:` values must
    be channels, which is why the counts leave here as a value channel.
================================================================================
*/

workflow INPUT_CHECK {
    take:
    samplesheet     // String  : path to the samplesheet CSV (already validated by CsvUtils)
    image_column    // String  : column holding the image this step consumes
    auto_reference  // boolean : params.allow_auto_reference on the linear path; false for add_cycle,
                    //           whose reference is the prior run's and never a row in this sheet

    main:

    // Pre-count images and channels per patient for streaming groupTuple operations.
    // Callers run CsvUtils.validateInputCSV / validateInputSemantics before getting
    // here, so the sheet is known parseable at this point.
    def patient_counts = CsvUtils.countImagesPerPatient(samplesheet)
    def channel_counts = CsvUtils.countChannelsPerPatient(samplesheet, image_column, params.nuclear_markers, auto_reference)
    // The per-SLIDE emit-set. channel_counts above is derived from this same resolver,
    // so the group size and the files that arrive cannot disagree.
    def keep_channels_by_slide = CsvUtils.resolveKeptChannelsPerSlide(
        samplesheet, image_column, params.nuclear_markers, auto_reference)

    // THE reference decision, made once, here, from the samplesheet -- before any
    // channel exists and therefore before anything can depend on task timing.
    // subworkflows/local/registration.nf used to make it instead, from a
    // `.groupTuple()` result, i.e. from whichever slide finished preprocessing first;
    // see CsvUtils.resolveReferenceRows for what that cost. Resolving here also puts
    // it upstream of the first checkpoint writer, which is what lets the resolved
    // `is_reference=true` reach csv/preprocessed.csv instead of being lost.
    def reference_image = CsvUtils.resolveReferenceRows(samplesheet, image_column, auto_reference)
    // Declared union -- FINAL_QC's run_summary.json manifest only, NEVER meta.channels_count.
    // See CsvUtils.countDeclaredChannelsPerPatient's doc for why the manifest cannot share
    // channel_counts' source.
    def declared_channel_counts = CsvUtils.countDeclaredChannelsPerPatient(samplesheet)

    ch_samples = Channel
        .fromPath(samplesheet, checkIfExists: true)
        .splitCsv(header: true)
        .map { row ->
            def meta = CsvUtils.parseMetadata(row, params.nuclear_markers, "CSV ${samplesheet}")
            // Overwrite the row's declared is_reference with the RESOLVED one. For a
            // sheet that declares a reference these agree by construction (rule 1 of
            // resolveReferenceRows returns that very row); they differ only where the
            // sheet declares none and auto-promotion applies, which is exactly the case
            // that must not be left to a downstream guess. A patient with no resolvable
            // reference (add_cycle's by-design zero-reference sheet) matches nothing and
            // keeps every row false.
            meta.is_reference = reference_image[meta.patient_id] != null &&
                                reference_image[meta.patient_id] == row[image_column]?.toString()?.trim()
            // Per-image unique id (patient_id + source-image stem). Drives output
            // file naming so a patient's multiple images do not produce identically
            // named files that collide when collected downstream (QC, registration).
            def stem = file(row[image_column]).simpleName
            meta.id = stem.startsWith(meta.patient_id) ? stem : "${meta.patient_id}_${stem}"
            return tuple(meta, file(row[image_column]))
        }

    // Add images_count to meta for streaming groupTuple
    if (patient_counts) {
        ch_image_counts = Channel.fromList(patient_counts.collect { k, v -> [k, v] })
        ch_samples = ch_samples
            .map { meta, f -> [meta.patient_id, meta, f] }
            .combine(ch_image_counts, by: 0)
            .map { _patient_id, meta, f, count ->
                // `+` creates a new top-level map, but Groovy's Map.plus() is
                // cloneSimilarMap(left).putAll(right) -- clone-then-putAll,
                // operationally identical to clone(). meta.channels is still
                // the same List reference as in the original meta. See
                // subworkflows/local/adapters/valis_adapter.nf:82-88 for why
                // that matters and why toSorted() (not sort()) is mandatory
                // wherever meta.channels is read.
                [meta + [images_count: count], f]
            }
    }

    // Add channels_count to meta for streaming groupTuple in postprocessing
    if (channel_counts) {
        ch_marker_counts = Channel.fromList(channel_counts.collect { k, v -> [k, v] })
        ch_samples = ch_samples
            .map { meta, f -> [meta.patient_id, meta, f] }
            .combine(ch_marker_counts, by: 0)
            .map { _patient_id, meta, f, count ->
                // See the Map.plus() note above.
                [meta + [channels_count: count], f]
            }
    }

    // Add keep_channels to meta: the exact channels THIS SLIDE emits.
    //
    // Deliberately a plain .map over a captured lookup, NOT the `.combine(ch, by: 0)`
    // pattern the two injections above use. Those key on patient_id; this is per-slide.
    // They are also INNER joins, and the guard below records that a key mismatch
    // silently DROPS the row -- the exact mechanism of the samplesheet-row-drop bug
    // already fixed once in this file. A .map cannot drop a row.
    ch_samples = ch_samples
        .map { meta, f ->
            // Fall back to the slide's full declared channel list, never to an empty
            // list: an empty keep-set would silently emit nothing, whereas over-emitting
            // is the recoverable direction (subworkflows/local/quantify_markers.nf).
            def keep = keep_channels_by_slide[meta.patient_id]?.get(f.name) ?: meta.channels
            // See the Map.plus() note above.
            [meta + [keep_channels: keep], f]
        }

    // Fail-fast guard: `.combine(by: 0)` above is an INNER join keyed on
    // patient_id. If a row's patient_id does not exactly match a key in the
    // pre-computed counts map (e.g. stray whitespace CsvUtils.parseMetadata
    // failed to trim), the row is silently dropped with no warning or error —
    // that is the exact mechanism of the samplesheet-row-drop bug this guard
    // closes. Compare what actually reaches the channel against the
    // independently-computed per-patient image total and error loudly (not
    // log.warn) on any shortfall, naming the totals so the cause is obvious.
    if (patient_counts) {
        def expected_count = patient_counts.values().sum() ?: 0
        ch_samples.count().subscribe { emitted ->
            if (emitted < expected_count) {
                error "INPUT_CHECK(${samplesheet}): ${expected_count - emitted} row(s) were silently dropped " +
                      "joining on patient_id (expected ${expected_count} row(s), got ${emitted}). This means a " +
                      "row's patient_id does not exactly match the value used to pre-compute per-patient counts " +
                      "(e.g. leading/trailing whitespace) so the inner-join combine(by: 0) discarded it."
            }
        }
    }

    emit:
    // [meta, file] — meta carries images_count and channels_count.
    samples = ch_samples
    // The same totals, for the QC report's sample manifest. A value channel
    // because `emit:` must be a channel; FINAL_QC unwraps it. Emitting them
    // here (rather than re-counting at the report site) keeps ONE count per run,
    // so the per-patient groupings and the manifest can never independently drift.
    //
    // `channels` and `declared_channels` are BOTH carried through on purpose, and
    // FINAL_QC must not blur them back into one number: `channels` is the exact,
    // post-nuclear-drop count that sized meta.channels_count/the groupKey above;
    // `declared_channels` is the samplesheet's raw union, for the manifest, which
    // should report what was declared rather than what survived quantification's
    // nuclear-channel drop. See CsvUtils.countChannelsPerPatient /
    // countDeclaredChannelsPerPatient for the full story of why they differ.
    counts  = Channel.value([
        patients: patient_counts,
        channels: channel_counts,
        declared_channels: declared_channel_counts,
    ])
}
