# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
responsibly. **Do not open a public GitHub issue for security vulnerabilities.**

Instead, please use one of the following methods:

- **GitHub Security Advisories (preferred):**
  [Report a vulnerability](https://github.com/Seekr-Technologies/seekr-hatchery/security/advisories/new)
  via GitHub's private reporting feature.
- **Email:** Send details to [lgrado@seekr.com](mailto:lgrado@seekr.com).

When reporting, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce the issue
- Any relevant logs, screenshots, or proof-of-concept code

## Response Timeline

- **Acknowledgment:** We will acknowledge receipt of your report within
  **48 hours**.
- **Initial assessment:** We aim to provide an initial assessment within
  **7 days** of acknowledgment.
- **Coordinated disclosure:** We follow a coordinated vulnerability disclosure
  process. We ask that you allow up to **90 days** from the initial report
  before any public disclosure, so that we have adequate time to develop and
  release a fix.

## Supported Versions

Security patches are applied to the **latest released version** only. We
recommend that all users keep their installations up to date.

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |
| Older   | No        |

## Scope

The following are considered security issues for this project:

- Remote code execution or command injection
- Sandbox escapes (e.g., a task agent breaking out of its Docker container or
  worktree isolation)
- Path traversal or unauthorized file access
- Dependency vulnerabilities with a demonstrated exploit path

General bugs, feature requests, and questions should be filed as regular
[GitHub issues](https://github.com/Seekr-Technologies/seekr-hatchery/issues).
