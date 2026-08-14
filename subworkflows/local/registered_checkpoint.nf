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
                       registered; see meta.is_passthrough below).
        ch_expected_rows: [patient_id, n] — how many slides this patient contributes
                       to the manifest, so CHECKPOINT_WRITER can publish that
                       patient's fragment the moment it is complete rather than at
                       channel close.

                       It comes from the CALLER's own group ([pid, ref, all_items] —
                       `items.size()`), not from meta.images_count, and that is not a
                       stylistic choice: under mode='add_cycle' the registered stream
                       carries TWO rows for a patient (the frozen prior reference plus
                       the new cycle) while the new slide's meta.images_count is 1 and
                       the synthesised reference meta has no images_count at all. A
                       size hint of 1 would close the group after one row and the
                       second row would open a second group, whose fragment overwrites
                       the first. The group size is the only count that is right on
                       both callers' paths.

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
            // Where the file WILL be published. This must agree with REGISTER's /
            // TILED_*'s publishDir in conf/modules.config, including the producer
            // subdirectory those blocks' `pattern:` carries along ('registered_slides/'
            // for VALIS, 'registered/' for tiled). Both rules live in Layout.
            //
            // A passthrough slide was never registered, so no registration process
            // published it and <pid>/registered/ may not even exist; Layout.passthroughPath
            // records where it actually is instead.
            def published_path = meta.is_passthrough
                ? Layout.passthroughPath(params.outdir, meta.patient_id, file)
                : Layout.publishedPath(params.outdir, meta.patient_id, Layout.REGISTERED, file)
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
