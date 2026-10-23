# Task: mudhra

**Status**: complete
**Branch**: mudhra
**Created**: 2026-10-23 09:07

## Objective

How does the mudhra cert issue affect hatchery users? Do we need to take mitigating steps?

## Summary

Eight eMudhra intermediate CA private keys generated in April 2025 were trivially
recoverable. A network-positioned attacker could therefore impersonate arbitrary
TLS services to non-browser clients that still trusted the affected chains.
Browser vendors blocked the keys quickly, but command-line tools, language
runtimes, and stale container trust stores required updates.

Hatchery is affected through tools running inside its sandbox and through native
agent use. Sandboxed package managers, Git, curl, Node, and optional uv could
accept a forged chain when their runtime or CA bundle was stale. Sandboxed agent
API traffic normally terminates TLS in Hatchery's host-side credential proxy,
whose `truststore.SSLContext` follows the host OS trust policy; S3 and OCI
sidecars use the same pattern. Users must still keep the host OS and native tools
updated.

The generated sandbox remains based on Debian trixie and installs
`ca-certificates` during the image build. Debian fixed trixie in
`20250419+deb13u1`, so a fresh no-cache build consumes the corrected trust
store without Hatchery-specific certificate logic. We deliberately avoided
embedding incident-specific version assertions in the starter Dockerfile.

Decisions and changes:

- `src/seekr_hatchery/agents/codex.py`: install Codex's Node runtime from the
  same NodeSource 22 channel used by Pi. Current Node 22 releases include the
  eMudhra distrust fix; this also avoids Debian's older bundled Node trust data.
- `src/seekr_hatchery/resources/Dockerfile.template`: update the optional uv
  image from vulnerable `0.10.7` to fixed `0.13.5`.
- `.hatchery/Dockerfile.codex` and `.hatchery/Dockerfile.pi`: apply the same
  changes to this repository's own sandbox definitions.
- `tests/test_agent_codex.py` and `tests/test_filesystem.py`: cover the
  NodeSource installation path and fixed uv template pin.
- `pyproject.toml`: require uv 0.13.5 or newer so local development cannot
  proceed with uv's affected bundled trust store. CI's pinned `setup-uv` action
  resolves this constraint to the latest compatible uv and runs on Node 24.

Existing generated Dockerfiles are user-owned copies and are not migrated
automatically. They should receive equivalent edits, then be rebuilt with
`--rebuild-sandbox` so stale `ca-certificates`, Node, and uv layers are not
reused.

Validation:

- uv 0.13.5 `lock --check`: passed; uv 0.10.7 is rejected by the configured floor.
- Ruff formatting for changed Python files and `ruff check .`: passed.
- Focused tests: 229 passed.
- Suite excluding kubectl cryptography tests: 1,310 passed, 22 skipped.
- The complete suite reached the kubectl TLS tests but the container's
  `cryptography` extension exited with an illegal-instruction error on arm64;
  this is an environment/native-wheel failure unrelated to these template-only
  changes.
- Repository-wide `ruff format --check .` also reports pre-existing formatting
  issues in `AGENTS.md` and
  `.hatchery/tasks/2026-06-02-reduce-virtiofs-burden.md`.

Primary references:

- Mozilla incident: https://bugzilla.mozilla.org/show_bug.cgi?id=2083449
- Google response: https://blog.google/security/protecting-chrome-and-android-users-from-compromised-emudhra-intermediate-ca-certificates/
- Debian trust-store fix: https://bugs.debian.org/1151413
- Ecosystem fixed-version tracker: https://emudhra.we-have-a-problem.com/
