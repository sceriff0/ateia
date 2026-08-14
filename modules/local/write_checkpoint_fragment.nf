/*
 * WRITE_CHECKPOINT_FRAGMENT - publish ONE patient's rows of ONE checkpoint
 *
 * The durable half of the checkpoint. `<outdir>/csv/<step>.csv` is written by
 * collectFile(), an operator that writes once, when the upstream channel closes --
 * that is, when the LAST patient finishes. Everything between "patient 3 of 20
 * finished" and "patient 20 finished" is a window in which the run's index to its own
 * published work exists only in the driver JVM's memory, and a walltime kill on the
 * head job takes it. This process closes the window: it is a TASK, so publishDir puts
 * its output on disk the moment that one patient's step completes.
 *
 * It writes a file and nothing else. The step's header, the row grammar and the
 * assembly of the two are lib/Checkpoint.groovy's; where the file lands is
 * lib/Layout.groovy's (mirrored by conf/modules.config's publishDir, which cannot see
 * lib/ classes). Both `script:` and `stub:` render from the SAME Checkpoint.fragment()
 * call for the reason lib/ProcessEnvelope exists: `-stub` never evaluates a
 * `script:` block, so a separately-written stub block is a second implementation the
 * blocking CI gate cannot see diverge -- and here the stub block is the ONE that CI
 * actually runs.
 *
 * Input:  [step, patient_id, rows]  -- rows are pre-rendered Checkpoint.row() strings
 * Output: <patient_id>.csv, published into <outdir>/csv/<step>.parts/
 */
process WRITE_CHECKPOINT_FRAGMENT {
    tag "${step}:${patient_id}"

    // NO label, deliberately. Resources come from the `withName:` block in
    // conf/modules.config, which sets all three fields -- one owner, not two
    // (tests/test_resource_label_coverage.py fails on both a label PLUS full withName
    // coverage and on neither). The label this carried was `process_single`, which on
    // this repo means 12 GB and 8 h; a 20-patient run queues four of these tasks per
    // patient, so it was reserving 960 GB-hours of scheduler allocation to write a few
    // hundred bytes of CSV -- a cost regression on the exact axis this process exists
    // to improve. The withName block asks for what a `cat` actually needs.

    // Bash-only, like AGGREGATE_SIZE_LOGS: the whole task is `cat > file`. No Python
    // interpreter is involved, which is why the versions.yml heredoc below is hand-
    // written (ProcessEnvelope always prepends a `python:` row) -- the second and only
    // other documented entry in tests/test_versions_envelope.py's ALLOWED_HANDWRITTEN.
    container 'ubuntu:22.04'

    input:
    tuple val(step), val(patient_id), val(rows)

    output:
    tuple val(step), val(patient_id), path("${Layout.checkpointFragmentName(patient_id)}"), emit: fragment
    path "versions.yml"                                                                    , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    // Column zero, deliberately: `body` is a multi-line block and every line after the
    // first lands verbatim in .command.sh. Interpolating it at an indented position
    // would indent only its FIRST line and corrupt the CSV. Nextflow's stripIndent()
    // then finds a minimum indent of 0 and strips nothing, which is what puts the
    // quoted heredoc terminator at column 0 where bash needs it. Same rule as
    // lib/ProcessEnvelope's returned blocks.
    def body = Checkpoint.fragment(step, rows)
    """
cat > ${Layout.checkpointFragmentName(patient_id)} <<'CHECKPOINT_FRAGMENT'
${body}CHECKPOINT_FRAGMENT

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        bash: \$(bash --version | head -n1 | sed 's/GNU bash, version //')
    END_VERSIONS
    """

    stub:
    // NOT a placeholder. The fragment IS the artifact -- there is no computation to
    // stand in for -- and the checkpoint tests read its contents out of stub runs,
    // which is the only mode CI's blocking gate runs. Rendered from the same
    // Checkpoint.fragment() call as script: above.
    def body = Checkpoint.fragment(step, rows)
    """
cat > ${Layout.checkpointFragmentName(patient_id)} <<'CHECKPOINT_FRAGMENT'
${body}CHECKPOINT_FRAGMENT

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        bash: stub
    END_VERSIONS
    """
}
