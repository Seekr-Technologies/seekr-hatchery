# Task: pi-per-model-proxy

**Status**: complete
**Branch**: pi-per-model-proxy
**Created**: 2026-11-05 13:47

## Objective

Fix Pi sandbox proxy routing so individual model URL paths are preserved.

## Summary

- Adapted the routing implementation from `logangrado/seekr-hatchery` branch `claude`, commit `5ff298bd2bbc57feb033832e625b122a3e2d6289`. Unrelated global-prompt and subagent-mount changes were deliberately excluded.
- Retained one proxy per provider. Host discovery considers effective model URLs (model definition → provider URL → catalog URL) and rejects differing upstream origins before launch. Separate providers are the workaround for distinct origins; this avoids silently forwarding credentials or requests to the wrong upstream.
- `src/seekr_hatchery/agents/pi.py` now passes a path-free proxy origin. The host catalog is mounted read-only under `hatchery-host-models-store.json`, not over the generated catalog.
- `src/seekr_hatchery/resources/pi_wrapper.sh` writes a proxy-routed catalog into the task volume, retaining individual model paths and custom metadata. Provider-level URL overrides are preserved when explicit and synthesized only for uniform effective model URLs. Generated authentication still contains only proxy credentials, including OAuth-shaped entries.
- The wrapper exports `PI_OFFLINE=1` to prevent catalog refresh from undoing routing. Catalog replacement uses an atomic rename so stale read-only files from older launches do not prevent resume; session state and host catalog remain untouched. URL queries/fragments are not copied into generated proxy URLs.
- Updated `README.md` with catalog handling and the single-origin restriction. Regression coverage in `tests/test_agent_pi.py` exercises model paths, override precedence, origin host/port/scheme rejection, catalog replacement on resume, offline inheritance, metadata preservation, and fake credentials using the real shell/Node wrapper with a Pi executable shim.

### Validation

- Final focused run: **240 passed** (65 Pi tests plus API-sidecar and Docker tests).
- Broader suite excluding `tests/test_kubectl_sidecar.py`: **1330 passed, 22 skipped**, before three final origin test variants were added and passed in the focused run.
- Full suite attempted but terminated with `Illegal instruction` while importing native `cryptography` for an unrelated Kubernetes certificate test in this container.
- `uv run ruff check .`, `uv run ruff format --check src tests`, and `git diff --check` pass. Repository-wide formatting additionally flags pre-existing Markdown code blocks in `AGENTS.md` and an older task record; these were left unchanged.
