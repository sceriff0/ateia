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
    def keep_channels_by_slide = CsvUtils.resolveKeptChannelsPerSlide(samplesheet, image_column, params.nuclear_markers, auto_reference)

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

    // Meta.identityFor's collision inputs. stem_counts is how it learns that two rows
    // of one patient would otherwise be assigned the SAME id -- the ordinary cyclic-IF
    // layout, cycle1/slide.ome.tiff + cycle2/slide.ome.tiff, both give the stem "slide".
    // row_index_by_key is what disambiguates them, and is computed from the SAMPLESHEET
    // itself, in samplesheet order -- never from channel arrival order below, which
    // `groupTuple` downstream is free to reorder and which resume caching cannot depend
    // on. Both are keyed the same way resolveKeptChannelsPerSlide already keys its map:
    // "patientId::rawImageCell" / row-scoped on that same raw cell -- never a basename.
    //
    // row_index_by_key's VALUE is a LIST per key, not a scalar -- see
    // CsvUtils.rowIndexPerPatient's doc. "patientId::rawImageCell" is not unique when
    // two rows share a raw cell (a duplicate row, or both blank), so the `.map` below
    // MUST consume by popping the front of the matching list on every row it processes
    // -- never by re-reading the same list entry -- which is what correctly reunites
    // each row with its own index despite the shared key.
    def stem_counts       = CsvUtils.stemCountsPerPatient(samplesheet, image_column)
    def row_index_by_key  = CsvUtils.rowIndexPerPatient(samplesheet, image_column)

    // Everything Meta.fromSamplesheetRow needs, pre-computed ONCE for the whole sheet
    // before any row is built -- ctx.channelsCount/imagesCount/stemCounts all throw on
    // a missing patient (see lib/Meta.groovy), so a partial ctx fails loudly here rather
    // than defaulting silently downstream.
    def meta_ctx = [
        keepChannelsBySlide: keep_channels_by_slide,
        imagesCount        : patient_counts,
        channelsCount      : channel_counts,
        stemCounts         : stem_counts,
    ]

    ch_samples = Channel
        .fromPath(samplesheet, checkIfExists: true)
        .splitCsv(header: true)
        .map { row ->
            def raw_image  = row[image_column]?.toString()?.trim()
            def patient_id = row.patient_id?.toString()?.trim()
            // Pop the FRONT of this key's index list, not a plain lookup: two rows can
            // share "patientId::rawImage" (see CsvUtils.rowIndexPerPatient's doc), and
            // popping is what reunites each row with its own file-order-derived index
            // instead of both rows reading back the same one. Safe here specifically
            // because splitCsv (above) reading a single file emits in file order,
            // deterministically -- this `.map` is chained directly off it with nothing
            // in between that could reorder rows.
            def row_indices = row_index_by_key["${patient_id}::${raw_image}".toString()]
            def row_index   = (row_indices && !row_indices.isEmpty()) ? row_indices.remove(0) : 0

            def meta = Meta.fromSamplesheetRow(row, image_column, row_index, meta_ctx)

            // Overwrite the row's declared is_reference with the RESOLVED one. For a
            // sheet that declares a reference these agree by construction (rule 1 of
            // resolveReferenceRows returns that very row); they differ only where the
            // sheet declares none and auto-promotion applies, which is exactly the case
            // that must not be left to a downstream guess. A patient with no resolvable
            // reference (add_cycle's by-design zero-reference sheet) matches nothing and
            // keeps every row false. `+`, never direct mutation of a field Meta already
            // set -- see subworkflows/local/adapters/valis_adapter.nf's toSorted() note
            // for why meta.channels stays a shared List reference across derived metas.
            meta = meta + [is_reference: reference_image[meta.patient_id] != null &&
                                          reference_image[meta.patient_id] == raw_image]

            return tuple(meta, file(row[image_column]))
        }

    // Fail-fast guard: independently compare what actually reached the channel against
    // the per-patient image total CsvUtils.countImagesPerPatient computed from the same
    // sheet, and error loudly (not log.warn) on any shortfall, naming the totals so the
    // cause is obvious. This used to be the only defense against a row silently vanishing
    // through a `.combine(by: 0)` inner join keyed on patient_id (removed above: every
    // row's id/counts now come from ONE Meta.fromSamplesheetRow call per row, with no
    // join to mis-key). Meta.fromSamplesheetRow's own ctx checks (lib/Meta.groovy) throw
    // synchronously on a patient absent from channelsCount/imagesCount, so that specific
    // failure mode is now unreachable too -- this guard is kept as a second, independent
    // check against any other cause of row loss (e.g. a malformed CSV line `splitCsv`
    // silently skips).
    if (patient_counts) {
        def expected_count = patient_counts.values().sum() ?: 0
        ch_samples.count().subscribe { emitted ->
            if (emitted < expected_count) {
                error "INPUT_CHECK(${samplesheet}): ${expected_count - emitted} row(s) did not reach the " +
                      "sample channel (expected ${expected_count} row(s), got ${emitted}). Check the sheet " +
                      "for malformed or unparseable rows."
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
