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

    Output:
        csv: the collected registration checkpoint manifest
========================================================================================
*/

workflow REGISTERED_CHECKPOINT {
    take:
    ch_registered   // [meta, file]

    main:
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism)
    ch_checkpoint_csv = ch_registered
        // Not written at a cleaning level: the registered slides are not published
        // there, so every row would name a file that does not exist. Gated HERE,
        // once, rather than at each call site -- which is the reason this writer is
        // its own file. See Checkpoint.writesAtLevel, and preprocess.nf for why a
        // filter is safe in front of a seeded collectFile.
        .filter { Checkpoint.writesAtLevel(Layout.REGISTERED, params.cleanup_level) }
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
            Checkpoint.row(Layout.REGISTERED, [
                patient_id      : meta.patient_id,
                // RULING R17: carried forward from meta, never re-derived from
                // registered_image's basename below -- see lib/Checkpoint.groovy.
                id              : meta.id,
                registered_image: published_path,
                is_reference    : meta.is_reference,
                channels        : meta.channels.join('|'),
            ])
        }
        .collectFile(
            name: Layout.checkpointCsvName(Layout.REGISTERED),
            newLine: true,
            sort: true,
            // sort: true makes the manifest REPRODUCIBLE. Without it collectFile
            // writes rows in completion order, so two runs of the same commit
            // produced different files (found while capturing this branch's golden
            // baseline; a rerun of the UNMODIFIED branch differed from itself). The
            // rows begin with patient_id followed by the published path, so natural
            // string order IS "patient id, then file" — and the `seed:` header is
            // written first regardless of sorting.
            storeDir: Layout.checkpointDir(params.outdir),
            seed: Checkpoint.header(Layout.REGISTERED)
        )

    emit:
    csv = ch_checkpoint_csv
}
