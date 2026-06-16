# Security Policy

## Supported Versions

MIRAGE is pre-1.0. Security fixes are applied to the `main` branch and shipped
in the next tagged release (`vX.Y.Z`). Older tags are not back-patched.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| tagged releases | latest only |

## Reporting a Vulnerability

Please **do not** open a public issue for security problems.

- Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  ("Report a vulnerability" under the repository's **Security** tab), or
- email the maintainers directly.

Please include: affected component (process/module/script), reproduction steps,
and impact. We aim to acknowledge within 5 working days.

## Secrets & Site Configuration

This pipeline is designed so that **no secret ever enters version control**:

- Site-specific configuration and credentials live in `conf/ieo.config` and
  `conf/*_site.config`, which are git-ignored and must stay local.
- CI/CD authenticates exclusively through the built-in `GITHUB_TOKEN` and
  repository **Secrets** (e.g. `CODECOV_TOKEN`) — never hardcoded values.
- Access tokens consumed by processes (e.g. `DEEPCELL_ACCESS_TOKEN`) must be
  passed via environment/secret injection at runtime, not committed.

If you discover a credential committed to history, treat it as compromised:
rotate it immediately and report it through the channel above.
