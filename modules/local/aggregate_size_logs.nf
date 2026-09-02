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
    container 'ubuntu:22.04'

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
