/*
========================================================================================
    SUBWORKFLOW: REGISTERED_CHECKPOINT
========================================================================================
    Writes the registration checkpoint manifest (Layout.REGISTERED, under
    Layout.checkpointDir) from a `[meta, file]` registered stream.

    WHY THIS IS ITS OWN FILE. The manifest is not a nicety of the linear path: it
    is the file `--start postprocessing` reads, and it is the file
    `mode='add_cycle'` reads out of `--prior_outdir` to recover the frozen
    reference. subworkflows/local/registration.nf owned the only copy, so an
    add_cycle run — which never goes through REGISTRATION — wrote no manifest at
    all, and its `--outdir` could therefore never be the `--prior_outdir` of a
    second add_cycle. Making the writer mode-independent is what closes that.

    The row format is the contract with every reader (add_cycle.nf's `ch_prior_ref`,
    CsvUtils' checkpoint validation, the `--start` samplesheet parser). It is owned by
    lib/Checkpoint.groovy — this file names the columns nowhere, it asks for them.

    Input:
        ch_registered: [meta, file] — every slide that reached the registered
                       stream, INCLUDING passthroughs (a slide that was never
                       registered). REGISTER_PATIENT has already published each of
                       them into <pid>/registered/registered_slides/, so this file
                       records one path rule and no longer branches on provenance.
        ch_expected_rows: [patient_id, n] — how many slides this patient contributes
                       to the manifest, so CHECKPOINT_WRITER can publish that
                       patient's fragment the moment it is complete rather than at
                       channel close.

                       It comes from the CALLER's own group ([pid, ref, all_items] —
                       `items.size()`), not from meta.images_count, and that is not a
                       stylistic choice: under mode='add_cycle' the registered stream
                       carries TWO rows for a patient (the frozen prior reference plus
                       the new cycle) while the new slide's meta.images_count is 1 and
                       the reference row's is the PRIOR run's row count, read out of
                       that run's registered.csv — neither is 2. A size hint of 1 would
                       close the group after one row and the second row would open a
                       second group, whose fragment overwrites the first. The group
                       size is the only count that is right on both callers' paths.

    Output:
        csv: the collected registration checkpoint manifest
========================================================================================
*/

include { CHECKPOINT_WRITER } from './checkpoint_writer'

workflow REGISTERED_CHECKPOINT {
    take:
    ch_registered     // [meta, file]
    ch_expected_rows  // [patient_id, n]

    main:
    ch_rows = ch_registered
        .map { meta, file ->
            // Where the file WILL be published. ONE rule for every row -- warped or
            // passed through, VALIS or tiled -- because every slide on this stream was
            // emitted into `registered_slides/` by REGISTER, TILED_STITCH or
            // PUBLISH_PASSTHROUGH, and publishDir carries that producer subdirectory into
            // the published path. Layout owns both halves and REFUSES a file emitted
            // anywhere else, rather than recording a path nothing publishes.
            //
            // There used to be a branch here: `meta.is_passthrough ? passthroughPath :
            // publishedPath`, which recorded an unwarped slide under <pid>/preprocessed/.
            // Only the tiled adapter passes a multi-slide patient's reference through, so
            // that branch made the recorded tree a function of --registration_method.
            // is_passthrough still exists and still means "never warped"; it no longer
            // means "published somewhere else".
            def published_path = Layout.registeredPath(params.outdir, meta.patient_id, file)
            [meta.patient_id, Checkpoint.row(Layout.REGISTERED, [
                patient_id      : meta.patient_id,
                registered_image: published_path,
                is_reference    : meta.is_reference,
                channels        : meta.channels.join('|'),
            ])]
        }
        // combine(), not join(): ch_expected_rows holds ONE entry per patient and this
        // stream holds n, so a 1:1 join would consume the count on the first row and
        // silently drop the rest. Same shape add_cycle.nf uses to pair every new slide
        // with its patient's single frozen reference.
        .combine(ch_expected_rows, by: 0)
        .map { patient_id, row, expected_rows -> [patient_id, expected_rows, row] }

    CHECKPOINT_WRITER(Layout.REGISTERED, ch_rows)

    emit:
    csv       = CHECKPOINT_WRITER.out.csv
    fragments = CHECKPOINT_WRITER.out.fragments
    versions  = CHECKPOINT_WRITER.out.versions
}
