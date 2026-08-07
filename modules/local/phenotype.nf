/*
 * PHENOTYPE - per-patient constrained phenotyping (§5). Consumes merged_quant.csv +
 * morphology.csv + frozen model_config.json; emits phenotypes.csv + constraint_audit.csv + QC.
 */
process PHENOTYPE {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    tuple val(meta), path(merged_quant), path(morphology)
    path model_config

    output:
    tuple val(meta), path("phenotypes.csv")       , emit: phenotypes
    tuple val(meta), path("constraint_audit.csv") , emit: audit
    tuple val(meta), path("phenotype_qc.json")    , emit: qc
    path "versions.yml"                           , emit: versions
    path("*.size.csv")                            , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    input_bytes=\$(stat -L --printf="%s" ${merged_quant} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${merged_quant.name},\${input_bytes}" > ${meta.patient_id}.PHENOTYPE.size.csv

    phenotype_cells.py \\
        --merged_quant ${merged_quant} \\
        --morphology ${morphology} \\
        --model_config ${model_config} \\
        --out phenotypes.csv \\
        --audit constraint_audit.csv \\
        --qc phenotype_qc.json \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        numpy: \$(python -c "import numpy; print(numpy.__version__)" 2>/dev/null || echo "unknown")
        scipy: \$(python -c "import scipy; print(scipy.__version__)" 2>/dev/null || echo "unknown")
        pandas: \$(python -c "import pandas; print(pandas.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    echo "label,phenotype,candidates,n_candidates,tree_path,density_bin,outcome,empty_type,violated_constraint_id,provenance" > phenotypes.csv
    echo "id,markers,observed,nominal,density_corr,neighbour_contact_corr,verdict" > constraint_audit.csv
    echo '{}' > phenotype_qc.json
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.PHENOTYPE.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        numpy: stub
        scipy: stub
        pandas: stub
    END_VERSIONS
    """
}
