# Task: pi-per-model-proxy

**Status**: complete
**Branch**: pi-per-model-proxy
**Created**: 2026-11-05 13:47

## Objective

Fix Pi sandbox proxy routing so individual model URL paths are preserved.

## Summary

- Adapted routing from `logangrado/seekr-hatchery` branch `claude`, commit `5ff298bd2bbc57feb033832e625b122a3e2d6289`. Retained one proxy per provider, with effective model URL precedence: model definition → provider URL → catalog URL. Different origins produce a launch error; configure separate providers for them.
- Origin comparison normalizes implicit/explicit default ports (HTTPS :443, HTTP :80). Distinct non-default ports remain distinct; target authorities preserve those ports and IPv6 brackets. Ignoring ports entirely could misroute requests.
- Squashed and rebased onto local `expand-pi-mounts` at `aea66dfb62343e2c6e90f5311bcb0af92ddfbb27`, adopting its Pi package layout and shared host agent-directory mounts. The identical `/usr/bin/env` portability fix is already in that base, so it is not repeated in this commit.
- `src/seekr_hatchery/agents/pi/backend.py` supplies path-free proxy origins. In addition to the base's auth/model overlays, `pi-config/models-store.json` is a task-local single-file bind mount over the shared host catalog. Host catalog contents are snapshotted per launch into read-only `hatchery-host-models-store.json`; that input is shadowed even when no host catalog exists. Missing/regular/symlink catalog tests ensure the host catalog and similarly named host files are not modified.
- `src/seekr_hatchery/agents/pi/wrapper.sh` preserves individual model paths and metadata while generating proxy-only auth. Explicit provider overrides remain authoritative. Uniform effective URLs use a shared proxy URL; mixed paths materialize routed chat-model definitions with configured overrides and retain a proxy provider fallback for bundled models absent from the host catalog. URL queries/fragments are omitted from generated routes.
- Panel review caught an inherited reference defect: Pi can ignore rewritten store entries for static/stale catalogs. Explicit definitions are necessary because rewritten JSON alone does not prove Pi uses those routes. Real Pi-runtime tests cover Codex/OpenAI with stale/timestamp-free catalogs and bundled models missing from the supplied catalog.
- The wrapper exports `PI_OFFLINE=1` to prevent network refresh undoing routing. It writes the catalog in place, not through atomic rename: single-file bind mounts cannot be replaced with `rename()`. The older volume-file recovery workaround was removed during stack integration.
- **Migration decision:** no new versioned migration is required. Proxy auth/models/catalog files are regenerated on each launch and contain no user history to convert. The base's `migrate_container_state` lifecycle and V1→V2 session-history upgrade are unchanged; migration module and abstract lifecycle interface have no delta against the stack base.
- Updated `README.md` with catalog isolation, routing rules, and migration rationale. Tests live in `tests/agents/pi/test_backend.py`, matching the base's reorganized modules.
- CodeQL follow-up replaced a URL-prefix assertion with parsed scheme/hostname/port comparisons. Production behavior is unchanged by that test adjustment. Existing non-chat configuration and HTTPS-only upstream transport limitations remain unchanged.

### Validation

- Rebased cleanly onto the updated stack base `aea66df` (mount-link alias fixes). Focused Pi/migration/mount-links validation: **178 passed, 24 skipped**; lint and source/test formatting pass. The branch remains one commit above the base.
- Prior stacked suite excluding `tests/test_kubectl_sidecar.py`: **1401 passed, 34 skipped**. This includes routing, mount behavior, and migration unit/recovery tests. Live container tests skip when Podman is unavailable; real ModelRuntime tests ran against installed Pi and skip on systems without it.
- Earlier full-suite attempt terminated with `Illegal instruction` while importing native `cryptography` for an unrelated Kubernetes certificate test in this container; that file remains excluded from local broad runs.
- Six follow-up regression cases were proven failing before fixes and passing afterward: two default-port orderings and four real Pi runtime/catalog cases.
- Initial four-reviewer panel found P1 routing and P2 default-port issues; focused fresh follow-up review verified both resolved with no issues. Readability and security reviewers requested no additional changes. Stack integration was validated directly afterward.
- `uv run ruff check .`, `uv run ruff format --check src tests`, and `git diff --check` pass. CodeQL itself was not run locally; CI must confirm the alert clears. Existing unrelated Markdown code-block formatting was left unchanged.
