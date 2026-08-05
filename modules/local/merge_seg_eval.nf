/*
 * MERGE_SEG_EVAL - concatenate per-patient CSE metric JSONs into one CSV.
 */
process MERGE_SEG_EVAL {
    tag "seg_eval_merge"
    label 'process_low'

    container "bolt3x/attend_image_analysis:${params.segeval_tag}"

    input:
    path(seg_eval_jsons)

    output:
    path "segmentation_metrics.csv", emit: csv
    path "versions.yml"            , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    merge_seg_eval.py --inputs ${seg_eval_jsons} --out segmentation_metrics.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version 2>&1 | sed 's/Python //')
    END_VERSIONS
    """

    stub:
    """
    printf 'id,QualityScore\\np1,0.0\\n' > segmentation_metrics.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
    END_VERSIONS
    """
}
