/*
 * AGGREGATE_SIZE_LOGS - Collect and merge all input size logs
 *
 * Aggregates per-task size logs from all processes into a single CSV file
 * for post-run analysis of resource usage vs input size.
 *
 * Input: Collection of size.csv files from all processes
 * Output: Aggregated input_sizes.csv file
 */
process AGGREGATE_SIZE_LOGS {
    tag "aggregate"
    label 'process_single'
    container null  // Simple CSV concatenation: no dependencies, runs on executor node

    input:
    path(size_csvs)

    output:
    path("input_sizes.csv"), emit: aggregated
    path "versions.yml"    , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    echo "process,sample_id,filename,bytes" > input_sizes.csv
    cat ${size_csvs} >> input_sizes.csv

    echo "Aggregated \$(wc -l < input_sizes.csv) size log entries"

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        bash: \$(bash --version | head -n1 | sed 's/GNU bash, version //')
    END_VERSIONS
    """

    stub:
    """
    echo "process,sample_id,filename,bytes" > input_sizes.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        bash: stub
    END_VERSIONS
    """
}
