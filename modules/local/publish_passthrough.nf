/*
 * PUBLISH_PASSTHROUGH - put an UNWARPED slide where every registered slide lives
 *
 * A passthrough is a slide that reaches the registered stream without having been
 * registered. Two produce one, for unrelated reasons:
 *
 *   - REGISTER_PATIENT's single-slide branch. A patient with one image has nothing to
 *     register against; VALIS crashes on a lone image, so the reference IS the output.
 *   - TILED_ADAPTER's reference. The tiled method warps every moving slide INTO the
 *     reference's frame, so the reference defines the frame and is never resampled.
 *     (VALIS re-writes its reference into a common frame, so under VALIS there is no
 *     passthrough at all for a multi-slide patient.)
 *
 * WHY A PROCESS EXISTS FOR A FILE THAT IS ALREADY ON DISK. Before this, csv/registered.csv
 * recorded a passthrough at the path its ORIGINAL producer published it to --
 * <pid>/preprocessed/<name> -- while every warped slide was recorded under
 * <pid>/registered/registered_slides/<name>. The same logical slide therefore landed in a
 * different tree depending on --registration_method, and every consumer of that manifest
 * (--start segmentation's INPUT_CHECK, add_cycle's frozen prior reference, anything
 * external) had to know which backend wrote it. publishDir is the only mechanism Nextflow
 * gives for putting a file somewhere, and publishDir needs a task -- so there is a task.
 *
 * IT COSTS ONE PUBLISHED COPY, AND NOT ONE MORE. The output is a SYMLINK into the staged
 * input, and publishDir's `copy` mode resolves symlinks (that is what `copyNoFollow`
 * exists to opt out of), so the work directory never holds a second copy of a whole-slide
 * image -- only the published tree does. That published copy is the same cost the VALIS
 * path has always paid: under VALIS a patient's reference exists BOTH as
 * <pid>/preprocessed/<name> and as <pid>/registered/registered_slides/<name>. The tiled
 * path was not saving that copy on purpose; it was pointing its manifest at another
 * step's artifact.
 *
 * The `registered_slides/` output subdirectory is not decoration: publishDir carries a
 * producer's output subdirectory into the published path, and lib/Layout.groovy's
 * registeredPath() REQUIRES it -- it is what makes REGISTER's, TILED_STITCH's and this
 * process's outputs land in one directory. tests/test_layout.py checks the three against
 * Layout.REGISTERED_SUBDIR.
 *
 * Input:  [meta, image]  -- the unwarped slide
 * Output: [meta, registered_slides/<image>] -- the same file, published as a registered slide
 */
process PUBLISH_PASSTHROUGH {
    tag "${meta.patient_id}:${meta.channels.join('_')}"

    // NO label, deliberately: resources come from the `withName:` block in
    // conf/modules.config, which sets all three fields. One owner, not two --
    // tests/test_resource_label_coverage.py fails on a label PLUS full withName coverage
    // and on neither.

    // Bash-only, like WRITE_CHECKPOINT_FRAGMENT and AGGREGATE_SIZE_LOGS: the whole task is
    // `ln -s`. No Python interpreter is involved, which is why the versions.yml heredoc
    // below is hand-written (ProcessEnvelope always prepends a `python:` row) -- the third
    // and last documented entry in tests/test_versions_envelope.py's ALLOWED_HANDWRITTEN.
    container 'ubuntu:22.04'

    input:
    tuple val(meta), path(image)

    output:
    tuple val(meta), path("${Layout.REGISTERED_SUBDIR}/${image.name}"), emit: registered
    path "versions.yml"                                               , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    // The stub below is this block with ONE line changed -- the `bash:` version row, which
    // is a `bash --version` call here and the literal `stub` there. Everything the process
    // actually DOES is identical, and that is the point: there is nothing to stand in
    // for. The task's whole job is to make the file appear under registered_slides/, which
    // a stub run has to do too -- csv/registered.csv is written from the resulting path,
    // and tests/checkpoint_manifest.nf.test opens every path it names in stub runs (the
    // only mode CI's blocking gate runs). A stub that touched an empty file instead would
    // publish a zero-byte slide over the real one's name.
    """
    mkdir -p ${Layout.REGISTERED_SUBDIR}
    ln -s ../${image} ${Layout.REGISTERED_SUBDIR}/${image.name}

cat <<END_VERSIONS > versions.yml
"${task.process}":
    bash: \$(bash --version | head -n1 | sed 's/GNU bash, version //')
END_VERSIONS
    """

    stub:
    """
    mkdir -p ${Layout.REGISTERED_SUBDIR}
    ln -s ../${image} ${Layout.REGISTERED_SUBDIR}/${image.name}

cat <<END_VERSIONS > versions.yml
"${task.process}":
    bash: stub
END_VERSIONS
    """
}
