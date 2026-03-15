process CUSTOM_DUMPSOFTWAREVERSIONS {
    tag "versions"
    label 'process_single'

    container null

    input:
    path(versions)

    output:
    path "software_versions.yml"    , emit: yml
    path "software_versions_mqc.yml", emit: mqc_yml
    path "versions.yml"             , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    #!/usr/bin/env python3
    import yaml
    import platform
    from collections import OrderedDict
    from pathlib import Path

    # Aggregate all versions files
    all_versions = OrderedDict()
    for f in sorted(Path('.').glob('*.yml')):
        if f.name in ('software_versions.yml', 'software_versions_mqc.yml', 'versions.yml'):
            continue
        try:
            with open(f) as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict):
                all_versions.update(data)
        except Exception:
            pass

    # Write aggregated versions
    with open('software_versions.yml', 'w') as fh:
        yaml.dump(dict(all_versions), fh, default_flow_style=False)

    # Write MultiQC-compatible YAML
    with open('software_versions_mqc.yml', 'w') as fh:
        mqc = {
            'id': 'software_versions',
            'section_name': 'MIRAGE Software Versions',
            'section_href': 'https://github.com/sceriff0/mirage',
            'plot_type': 'html',
            'description': 'Software versions collected at run time.',
            'data': '<dl class="dl-horizontal">'
        }
        for process_name, tools in all_versions.items():
            if isinstance(tools, dict):
                for tool, version in tools.items():
                    mqc['data'] += f'<dt>{tool}</dt><dd><samp>{version}</samp></dd>'
        mqc['data'] += '</dl>'
        yaml.dump(mqc, fh, default_flow_style=False)

    # Self version
    with open('versions.yml', 'w') as fh:
        yaml.dump({
            '${task.process}': {
                'python': platform.python_version(),
                'pyyaml': yaml.__version__
            }
        }, fh, default_flow_style=False)
    """

    stub:
    """
    touch software_versions.yml
    touch software_versions_mqc.yml

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
    END_VERSIONS
    """
}
