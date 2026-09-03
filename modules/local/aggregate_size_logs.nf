/*
 * AGGREGATE_SIZE_LOGS - Collect and merge all input size logs
 *
 * Aggregates per-task size logs from all processes into a single CSV file
 * for post-run analysis of resource usage vs input size.
 *
 * Input: Collection of size.csv files from all processes
 * Output: Aggregated input_sizes.csv (header from ProcessEnvelope.SIZE_LOG_COLUMNS)
 */
process AGGREGATE_SIZE_LOGS {
    tag "aggregate"
    label 'process_single'
    // NOT a bare `ubuntu:22.04`. That base ships no procps, and Nextflow's task-metrics
    // wrapper hard-exits before the script: block without `ps` -- so with the shipped
    // params.enable_trace=true this process failed with exit 1 and empty stdout on every
    // real run. The preprocess image has bash, coreutils and procps, and is already
    // pulled by six other modules, so this costs the cluster no extra pull. The script
    // below is echo/cat/wc and a hand-written bash-only versions.yml; it needs nothing
    // else from the image.
    container 'bolt3x/mirage-preprocess:1.0.0'

    input:
    path(size_csvs)

    output:
    path("input_sizes.csv"), emit: aggregated
    path "versions.yml"    , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    echo "${ProcessEnvelope.SIZE_LOG_COLUMNS.join(',')}" > input_sizes.csv
    cat ${size_csvs} >> input_sizes.csv

    echo "Aggregated \$(wc -l < input_sizes.csv) size log entries"

    ${ProcessEnvelope.versionsBash(task.process, task.container)}
    """

    stub:
    """
    echo "${ProcessEnvelope.SIZE_LOG_COLUMNS.join(',')}" > input_sizes.csv
    cat ${size_csvs} >> input_sizes.csv

    ${ProcessEnvelope.versionsBashStub(task.process, task.container)}
    """
}
