# Security Policy

## Supported versions

Metis is in early public release (v0.1.x). Only the latest minor
release receives security updates. Until v1.0, all minor releases
are eligible for breaking changes.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security
vulnerabilities.

Use [GitHub Security Advisories](https://github.com/david-2814/metis/security/advisories/new)
to report privately.

We aim to:

- Acknowledge receipt within **3 business days**
- Provide an initial assessment within **7 business days**
- Disclose publicly only after a fix is available (coordinated
  disclosure)

## Scope

In scope:

- Code in this repository (`metis-core`, `metis-server`,
  `metis-gateway`, `metis-cli`)
- Published PyPI packages matching this repository

Out of scope:

- The paid-tier overlay (`metis-pro`) -- closed-source; report
  via the maintainers of that repo
- Third-party dependencies (report to upstream maintainers)
- Issues in user deployments (configuration, network, secrets
  management) -- these are operator responsibilities

## Disclosure process

1. Reporter submits via Security Advisories or email
2. We confirm reproducibility and assess severity (CVSS 3.1)
3. Fix lands on a private branch + tagged release
4. Public disclosure with CVE if applicable; reporter credited
   unless they prefer anonymity
