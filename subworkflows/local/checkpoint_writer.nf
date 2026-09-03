/*
========================================================================================
    SUBWORKFLOW: CHECKPOINT_WRITER
========================================================================================
    The ONE writer of a checkpoint manifest. Takes a step name and a channel of row
    Maps; writes `<outdir>/csv/<step>.csv`.

    WHY THIS EXISTS. The same five-step chain appeared four times, verbatim:

        .filter { Checkpoint.writesAtLevel(<STEP>, params.cleanup_level) }
        .map    { Checkpoint.row(<STEP>, [...]) }
        .collectFile(name: Layout.checkpointCsvName(<STEP>), newLine: true, sort: true,
                     storeDir: Layout.checkpointDir(params.outdir),
                     seed: Checkpoint.header(<STEP>))

    -- in preprocess.nf, registration.nf (via a since-deleted registered_checkpoint.nf),
    segmentation.nf and postprocessed_checkpoint.nf. Three of them additionally carried
    the SAME eight-line "sort: true is load-bearing" comment, word for word. Four copies
    of one mechanism is four chances for one of them to lose the cleanup gate, or the
    sort, or the seed -- and each loss is silent: the run stays green and the manifest
    is wrong or missing.

    WHAT STAYS WITH THE CALLERS, and why. The ROW is built by the caller, because
    building it is where the real per-step knowledge lives: which Layout `kind` an
    artifact publishes under, whether a passthrough slide needs Layout.passthroughPath,
    whether a mask came from this run (Layout.publishedPath) or a prior one
    (Layout.publishedOrAsIs). Those decisions differ per step and are not mechanism.
    Additionally, tests/test_layout.py statically scans real call sites for the
    shape "Layout.publishedPath, given params.outdir, a patient id, then a literal
    kind string" -- so folding those calls in here behind a variable would blind
    that guard, and writing the shape itself as a literal example in THIS file's
    comments would trip the very same scan (it does not distinguish a real call
    from a comment that merely illustrates one).

    So the split is: the CALLER decides what a row says; this file decides whether,
    where, in what order and under what header it is written.

    SORT IS LOAD-BEARING, WHICH IS WHY IT IS STATED HERE EXPLICITLY RATHER THAN LEFT
    TO collectFile's DEFAULT. `sort: true` already IS collectFile's default -- it is
    NOT what makes the manifest reproducible by itself, and an earlier version of this
    comment claimed the opposite (that omitting it would write completion order). What
    is load-bearing is that nobody sets `sort: false`: that option writes rows in
    COMPLETION order, so two runs of the same commit produce different files -- a rerun
    of an unmodified branch differing from itself is exactly the failure mode this
    guards against. Stating `sort: true` here means an edit has to delete or flip the
    option to lose the property, not merely fail to add one. Rows begin with
    patient_id followed by the published path, so natural string order IS "patient id,
    then file", and the `seed:` header is written first regardless of sorting.

    A CLEANING LEVEL WRITES NO FILE AT ALL. Not an empty one, not a header-only one.
    Checkpoint.writesAtLevel carries the full reasoning (and the observed dangling-path
    failure); the mechanism is that the SOURCE is filtered rather than the chain being
    wrapped in an `if`, because an empty channel into a seeded collectFile writes nothing
    and emits nothing -- verified on NXF_VER=26.04.6 rather than assumed, since a seeded
    collectFile that wrote a header-only manifest would have been exactly the dangling
    manifest the gate exists to prevent.

    THIS IS THE ONLY FILE UNDER subworkflows/ THAT READS params.cleanup_level, and the
    only one that names Layout.checkpointDir in a collectFile. Guarded by
    tests/test_checkpoint_writer_is_the_only_writer.py.

    NO ALIAS IS NEEDED for the four callers: each has its own `include` in its own file,
    which is a separate component instance -- the same shape POSTPROCESSED_CHECKPOINT
    already had across postprocess.nf and add_cycle.nf. This workflow declares no
    processes, so there is nothing for Nextflow's single-invocation rule to object to.

    Input:
        step:    one of Checkpoint.STEPS[*].name
                 ('preprocessed' | 'registered' | 'segmented' | 'postprocessed').
                 A plain String, not a channel -- Nextflow binds workflow `take:` values
                 verbatim, the same way REGISTER_PATIENT takes `method`.
        ch_rows: channel of Map (column -> value), ALREADY passed through
                 Layout.publishedPath / Layout.publishedOrAsIs by the caller.

    Output:
        csv: the collected manifest (an EMPTY channel at a cleaning level)
========================================================================================
*/

workflow CHECKPOINT_WRITER {
    take:
    step        // String: a Checkpoint.STEPS name
    ch_rows     // channel of Map: column -> value

    main:
    ch_checkpoint_csv = ch_rows
        .filter { Checkpoint.writesAtLevel(step, params.cleanup_level) }
        // Checkpoint.row orders the values by the DECLARED column list, so insertion
        // order in the caller's Map is irrelevant, and it throws on a missing or unknown
        // KEY rather than silently emitting an empty field. An empty field in a
        // checkpoint row is a path that does not exist -- the failure the postprocessing
        // manifest shipped with for two releases.
        .map { row -> Checkpoint.row(step, row) }
        .collectFile(
            name: Layout.checkpointCsvName(step),
            newLine: true,
            sort: true,
            storeDir: Layout.checkpointDir(params.outdir),
            seed: Checkpoint.header(step)
        )

    emit:
    csv = ch_checkpoint_csv
}
