/*
 * PREFLIGHT_SCALE - resolve `--pixel_size` for every input slide from OME metadata alone.
 *
 * Runs once, over EVERY slide the run is about to process, before any heavy work
 * (CONVERT_IMAGE staging, registration, ...) starts. It reads only OME headers -- never
 * pixel data -- so it is fast enough to run unconditionally: see bin/preflight_scale.py's
 * module docstring for why that is the whole point.
 *
 * `--pixel_size auto` with any slide carrying no usable OME PhysicalSizeX/Y fails this
 * task, which fails the run, before a single byte of the samplesheet's images is
 * otherwise touched. A supplied number instead gets a WARNING (a metadata disagreement,
 * or metadata that simply is not there to confirm it against) and the run proceeds.
 *
 * The `stub:` block below deliberately calls the SAME script as `script:`, unlike most
 * of this repo's stubs (see CLAUDE.md's "Verification reality" #1: "-stub never
 * evaluates a script: block"). This is the intentional exception: the whole point of
 * this process is a metadata-only check that is fast enough to run unconditionally, so
 * there is nothing to fake -- and `-profile test -stub` (CI's nextflow-stub job) is
 * exactly the run that must exercise the "number supplied, no metadata" WARNING path,
 * since conf/test.config pins a number against fixtures with no OME PhysicalSize.
 * Faking this stub would make that whole code path invisible to CI.
 */
process PREFLIGHT_SCALE {
    tag "preflight"
    label 'process_single'

    container "bolt3x/mirage-preprocess:1.0.0"

    input:
    path(images, stageAs: 'input_?/*')

    output:
    path("preflight_scale_report.json"), emit: report
    path "versions.yml"                , emit: versions
    path("*.size.csv")                 , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def pixel_size = params.pixel_size
    """
    # This task fans in over every slide in the run, so one aggregate row, sample id `all`.
    ${ProcessEnvelope.sizeLog(task.process, 'all', ['input_*/*'], 'PREFLIGHT_SCALE.size.csv')}

    preflight_scale.py \\
        --images \$(find -L input_* -maxdepth 1 -type f | sort) \\
        --pixel-size ${pixel_size} \\
        --output preflight_scale_report.json

    ${ProcessEnvelope.versions(task.process, ['tifffile'], task.container)}
    """

    stub:
    def pixel_size = params.pixel_size
    """
    ${ProcessEnvelope.sizeLogStub(task.process, 'all', 'PREFLIGHT_SCALE.size.csv')}

    preflight_scale.py \\
        --images \$(find -L input_* -maxdepth 1 -type f | sort) \\
        --pixel-size ${pixel_size} \\
        --output preflight_scale_report.json

    ${ProcessEnvelope.versionsStub(task.process, ['tifffile'], task.container)}
    """
}
