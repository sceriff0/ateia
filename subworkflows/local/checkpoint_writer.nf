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
                 close. Null is tolerated and means "unknown": the group then waits
                 for the channel to close, which is correct but not durable, and the
                 caller has given up the property this subworkflow exists for. Each
                 caller must supply it consistently for ALL of a patient's rows — a
                 mixture of sized and unsized keys for one patient would open two
                 groups and the second fragment would overwrite the first.

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
    ch_fragment_in = ch_rows
        .map { patient_id, expected_rows, row ->
            // groupKey(id, size) is a STREAMING SIZE HINT: groupTuple() closes the
            // group as soon as `size` items have arrived instead of waiting for the
            // whole channel. That is the entire mechanism by which a finished
            // patient's fragment is written while other patients are still running.
            [expected_rows ? groupKey(patient_id, expected_rows as int) : patient_id, row]
        }
        .groupTuple()
        .map { key, rows ->
            // UNWRAP THE groupKey immediately, in the very next operator — see
            // tests/test_group_key_unwrapped.py. GroupKey's equals() is asymmetric, so
            // letting the wrapper travel on makes any later join()/combine() succeed or
            // fail on arrival order.
            //
            // toSorted() for the same reason collectFile carries `sort: true`: rows
            // arrive in completion order, and Nextflow hashes a list input POSITIONALLY,
            // so an unsorted list makes an otherwise identical rerun miss the cache and
            // makes the published fragment differ from itself between runs. Sorting
            // strings that begin with patient_id then the published path gives the same
            // order the aggregate has.
            tuple(step, key.toString(), rows.toSorted())
        }

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
