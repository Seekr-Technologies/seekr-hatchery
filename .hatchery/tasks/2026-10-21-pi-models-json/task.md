# Task: pi-models-json

**Status**: complete
**Branch**: pi-models-json
**Created**: 2026-10-21 11:02

## Objective

Make host Pi `models.json` custom providers, model definitions, and overrides available in sandboxed Pi sessions without exposing host API keys to the container.

## Context

Hatchery previously generated the container's entire `~/.pi/agent/models.json` at launch so every configured provider used the credential proxy. That protected host credentials, but it discarded user-authored model definitions and prevented providers found only in `models.json` from being discovered and routed.

## Summary

- Host `models.json` is parsed with the same JSONC conventions as Pi: `//` comments and trailing commas are accepted.
- Provider discovery now layers `models.json` over `models-store.json`. Custom providers and endpoint overrides therefore receive normal Hatchery proxy endpoints and host-side credential resolution through `pi auth check`.
- Before each container launch, Hatchery writes a per-task sanitized model configuration. Provider `apiKey` values are removed, and common authentication headers are replaced with the per-session proxy token. The host credential-bearing file is never mounted directly.
- The sanitized file is mounted read-only beside Pi's generated configuration. `pi_wrapper.sh` now uses Node.js to preserve custom models, APIs, compatibility settings, schemas, and overrides while replacing each active provider's `baseUrl` and `apiKey` with proxy values.
- Host model changes take effect on the next launch or resume. Authentication and routing remain container-local and proxy-only.
- The repository Pi harness image is pinned to `@earendil-works/pi-coding-agent@1.3.0`.
- Wrapper tests prepend their fake `pi` shim to the existing host `PATH` instead of replacing it, so CI can find Node.js in non-system locations. They skip only on hosts where Node.js is genuinely unavailable; the Pi container always installs Node.js.
- Updated `README.md`, `src/seekr_hatchery/agents/pi.py`, `src/seekr_hatchery/resources/pi_wrapper.sh`, and `tests/test_agent_pi.py`.
- Validation: Ruff formatting and linting pass; all 1,294 tests outside `test_kubectl_sidecar.py` pass, with 22 skipped. The kubectl suite reaches its certificate fixture but the sandbox's ARM `cryptography` native module exits with an unrelated illegal-instruction error.
