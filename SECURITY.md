# Security Policy

## Supported Versions

ELKeeper is currently a development preview. Security fixes are applied only to
the latest revision of the default branch; there are no supported release
branches yet.

## Reporting A Vulnerability

Do not disclose a suspected vulnerability in a public GitHub issue or discussion.

Report it privately through GitHub's private vulnerability reporting feature for
this repository. Include the affected revision, impact, reproduction steps, and
the minimum redacted evidence needed to understand the issue.

Never include:

- Administrator or managed-host passwords
- Private keys, API keys, service tokens, or enrollment tokens
- Controller database files or encryption keys
- Unredacted Ansible inventories or run logs
- Publicly reachable deployment endpoints

## Deployment Notice

ELKeeper performs privileged operations over SSH and manages rootful Podman
workloads. Operators are responsible for reviewing the source and playbooks,
restricting controller access, using HTTPS, protecting persistent volumes, and
maintaining tested backups.
