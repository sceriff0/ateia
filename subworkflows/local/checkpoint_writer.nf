/*
========================================================================================
    SUBWORKFLOW: CHECKPOINT_WRITER
========================================================================================
    The one sink every checkpoint CSV goes through. Takes a stream of pre-rendered
    checkpoint rows and writes them TWICE, at two granularities:

      <outdir>/csv/<step>.parts/<patient_id>.csv   one patient's rows, published by
                                                   WRITE_CHECKPOINT_FRAGMENT as soon
                                                   as that patient's step finishes
      <outdir>/csv/<step>.csv                      every patient's rows, written by
                                                   collectFile() when the last one does

    WHY BOTH. collectFile() is an OPERATOR, not a task: it writes its file exactly once,
    when the upstream channel closes. So the aggregate only exists after the LAST patient
    finishes, and for the whole span before that the run's index to its own published
    work lives only in the driver JVM's memory. A walltime kill on the head job, an OOM,
    or a node failure in that window loses the index while every completed patient's
    output sits published on disk — the expensive half survives, the cheap half does not.
    The fragments make the record durable per patient; the aggregate stays the convenient
    artifact every existing reader already opens.

    (Measured, not assumed: a hard TASK failure does NOT lose the aggregate. Under
    conf/base.config's shipped errorStrategy — 'finish' for any non-signal exit — and
    under an explicit 'terminate', Nextflow shuts down in an orderly way, the channels
    close, and collectFile's completion handler fires. The gap is the time window, not
    the abort path. See tests/checkpoint_durability.nf.test.)

    ONE SCHEMA. Fragments and aggregate share lib/Checkpoint.groovy's header and row
    builder — the fragment writer is handed the SAME row strings the aggregate is, so
    there is nothing here that could render a second grammar.

    NOT DOWNSTREAM OF collectFile. WRITE_CHECKPOINT_FRAGMENT reads the per-patient row
    channel, not the aggregate. That is deliberate: a process consuming a collectFile()
    output can never cache (collectFile rewrites to a fresh work/tmp/<hash>/ every run),
    which is why GENERATE_QC_REPORT and AGGREGATE_SIZE_LOGS are declared `cache = false`.
    The fragment writer needs no such declaration because it consumes no such input.

    Input:
        step     String — a Layout.CHECKPOINT_STEPS member. A plain value, not a
                 channel, in the same way REGISTER_PATIENT takes `method`.
        ch_rows  [patient_id, expected_rows, row]
                 `row` is a rendered Checkpoint.row() string.
                 `expected_rows` is HOW MANY rows this patient contributes to this
                 checkpoint — the streaming size hint that lets a patient's fragment
                 be written the moment that patient is done instead of at channel
                 close. It is MANDATORY (a positive Integer): an absent or unusable
                 count ABORTS the run, naming this channel, rather than degrading to
                 a group that waits for the whole channel to close. Null used to be
                 tolerated as "unknown", which silently gave up the exact property
                 this subworkflow exists for on a run that still exited 0; see
                 lib/PatientGroup.groovy's header. Each caller must supply it
                 consistently for ALL of a patient's rows — a mixture of counts for
                 one patient would open two groups and the second fragment would
                 overwrite the first.

    Output:
        csv        the aggregate checkpoint CSV (unchanged in name, contents and byte
                   order from before this subworkflow existed)
        fragments  [step, patient_id, file] per-patient fragments
        versions
========================================================================================
*/

include { WRITE_CHECKPOINT_FRAGMENT } from '../../modules/local/write_checkpoint_fragment'

workflow CHECKPOINT_WRITER {
    take:
    step     // String — Layout.CHECKPOINT_STEPS member
    ch_rows  // [patient_id, expected_rows, row]

    main:
    // The sized groupKey, remainder:true, the GroupKey unwrap and the canonical
    // ordering are lib/PatientGroup.groovy's — read its header for what each one is
    // load-bearing for. This site used to hand-write all four, including the ternary
    // whose else-branch was an UNSIZED key: `expected_rows` was documented as
    // optional, and an absent one silently traded the whole property this
    // subworkflow exists for (a finished patient's fragment published while other
    // patients still run) for a full-run barrier, on a run that still exits 0.
    // It is now MANDATORY and an absent one aborts, naming this channel.
    //
    // ch_rows arrives as `[patient_id, expected_rows, row]`; PatientGroup takes the
    // repo's universal `[meta, payload]` shape, so the two per-patient facts are
    // reshaped into a meta here. `size:` (a meta KEY) then says it directly — no
    // `as int` coercion, because a count that is not an Integer is a producer bug
    // and requireSize says so rather than rounding it into one.
    //
    // sortBy for the same reason collectFile carries `sort: true`: rows arrive in
    // completion order, Nextflow hashes a list input POSITIONALLY, so an unsorted
    // list makes an otherwise identical rerun miss the cache AND makes the published
    // fragment differ from itself between runs. The rows begin with patient_id then
    // the published path, so natural String order is the order the aggregate has.
    ch_fragment_in = PatientGroup.byPatient(
            ch_rows.map { patient_id, expected_rows, row ->
                [[patient_id: patient_id, expected_rows: expected_rows], row]
            },
            name  : 'CHECKPOINT_WRITER: the per-patient checkpoint rows feeding WRITE_CHECKPOINT_FRAGMENT',
            size  : 'expected_rows',
            sortBy: { _meta, row -> row },
        )
        .map { patient_id, pairs -> tuple(step, patient_id, pairs.collect { pair -> pair[1] }) }

    WRITE_CHECKPOINT_FRAGMENT(ch_fragment_in)

    ch_csv = ch_rows
        .map { _patient_id, _expected_rows, row -> row }
        .collectFile(
            name: Layout.checkpointCsvName(step),
            newLine: true,
            sort: true,
            // sort: true makes the manifest REPRODUCIBLE. Without it collectFile writes
            // rows in completion order, so two runs of the same commit produced
            // different files. The rows begin with patient_id followed by the published
            // path, so natural string order IS "patient id, then file" — and the `seed:`
            // header is written first regardless of sorting.
            storeDir: Layout.checkpointDir(params.outdir),
            seed: Checkpoint.header(step)
        )

    emit:
    csv       = ch_csv
    fragments = WRITE_CHECKPOINT_FRAGMENT.out.fragment
    // `.first()` HERE, at the emitting boundary, not at each of the four call sites.
    // WRITE_CHECKPOINT_FRAGMENT runs once per patient; the QC report wants one row per
    // process. Emitting the raw per-task stream made every caller apply `.first()` to a
    // SUBWORKFLOW's versions -- which happens to be harmless only because this
    // subworkflow holds exactly one process, and would silently drop rows the moment it
    // held two. See tests/test_versions_cardinality.py.
    versions  = WRITE_CHECKPOINT_FRAGMENT.out.versions.first()
}
