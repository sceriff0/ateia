# Troubleshooting

## `Invalid --start` or `Invalid --registration_method`

Use only supported enums:

- Step: `preprocessing`, `registration`, `postprocessing`
- Registration: `valis`

## `--input` Validation Errors

`--input` is required for all steps (`preprocessing`, `registration`, `postprocessing`).

## Out-of-Memory / Runtime Failures

Actions:

1. Increase global caps (`max_memory`, `max_time`).
3. Check process-specific settings in `conf/modules.config`.
4. Resume with `-resume` after adjustments.

## Nextflow Not Found

If `nextflow` is not installed or not in `PATH`, install/activate it and retry:

```bash
nextflow -version
```

## Debug Channel Visibility

Set:

```bash
--debug_channels true
```

to enable channel-level debug `.view` output in subworkflows where supported.

