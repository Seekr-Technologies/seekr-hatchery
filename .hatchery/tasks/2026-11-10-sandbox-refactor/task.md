# Task: sandbox-refactor

**Status**: complete
**Branch**: sandbox-refactor
**Created**: 2026-11-10

## Objective

Pure package refactor directly on origin/main (dfbbabc), as exactly one refactor: commit. Preserve application behavior; startup optimization/fingerprinting will be a separate later change.

## Summary

### Decision and structure

Replaced top-level docker.py, mount.py, mount_links.py, and seeded_volumes.py with src/seekr_hatchery/sandbox/:

- mount_types.py: declarative mount models and seed context; dependency leaf.
- config.py: SandboxConfig, migration/overlay loading and validation, environment/features.
- scaffold.py: Dockerfile/config generation and DinD Dockerfile checks.
- spec.py: ContainerSpec and engine-neutral security/environment policy.
- runtime.py: Docker/Podman adapters, discovery, argv rendering, file-volume command wrapping, subprocess/PTY execution.
- images.py: build_image and build-output streaming. Always invokes the builder exactly as origin/main.
- mounts.py: task/shell mount assembly, includes/git metadata, safety, clipboard paths.
- mount_links.py: symlink-aware expansion.
- volumes.py: seeded/shared volume preparation, rollback and cleanup.
- launch.py: runtime-selection policy, launch context, task/shell coordination, sidecar/paste setup and cleanup.

The root exports only leaf mount types. Callers import defining submodules directly. Runtime does not import volume orchestration. Session identity/tokens/worktrees/config-edit workflows remain in sessions; agent behavior, sidecar implementations and generic terminal infrastructure stay outside sandbox.

Full design: docs/sandbox-refactor-plan.md.

### Compatibility decisions

No fingerprinting, image inspection/reuse, fingerprint labels, eager Dockerfile reads or new image-read errors are included. README is identical to origin/main. The optimization task record is excluded. The initial combined result is preserved only on local backup branch sandbox-refactor-with-fingerprinting.

Config/task schemas, filenames, CLI flags, resource names, argv/mount ordering, runtime preference, launch/cleanup order, seed-once/rollback semantics, and user-facing errors remain unchanged. SandboxConfig retains the DockerConfig model title to preserve error text and JSON schema. Original diagnostic logger identities (seekr_hatchery.docker, seekr_hatchery.mount_links, seekr_hatchery.seeded_volumes) are retained: names appear in formatted output and the Docker logger's DEBUG selector controls the image-build process path.

Resources remain at the existing package resources/ location; scaffold/spec paths account for the extra directory. Package imports avoid eager launch initialization through agents/sidecars. Volume cleanup imports runtime normally; task-volume naming retains a call-time sessions dependency. Internal module/class/function paths and associated introspection necessarily reflect the approved renaming; old Python import paths have no compatibility shims. launch_context retains its quoted forward annotation with qualified type name.

### Review and equivalence evidence

Bounded component-owner subagents performed initial extraction/integration. After the user clarified the base/single-commit requirement, parent removed all optimization work and rewrote the branch directly on origin/main. Three fresh reviewers then independently inspected correctness, tests and architecture against origin/main; no findings survived final review.

A separate command-capable read-only auditor executed baseline and refactor independently:

- 101 differential scenario pairs, zero discrepancies after substituting only generated temporary paths and relocated resource paths.
- Scenarios cover config/defaults/errors/overlays/schema, mount models, 32 full spec/argv combinations, 32 repeated-build success/failure/debug/cache combinations, 12 volume lifecycle/rollback cases, mount assembly, command overrides, generated scaffold files and seccomp content.
- 69 moved declarations: final parent rerun found 65 normalized-identical, four explained differences (explicit config title, shell-block extraction, runtime import/local-variable change, qualified call-time session import); none missing.
- All 21 class member routines and 138 changed external-consumer declarations normalized-identical.
- 16 constants/type aliases: 14 exact assignments, two expected resource-path relocations; all ten class-default inventories inspected.
- Parent independently reran the audit harness and inspected raw results. Scripts/raw JSON/report were retained outside the repository under /tmp/pure-refactor-audit/.

Parent identified a diagnostic-name difference in the audit limitations and restored original logger identities; also retained quoted annotation representation. A bounded reviewer follow-up verified that correction and reported no issues. Exact seed TypeError strings are preserved and asserted through public volume preparation.

### Validation

Direct origin/main and final refactor runs use the same environment:

- origin/main: 1,316 passed, 22 skipped, excluding tests/test_kubectl_sidecar.py.
- Refactor: 1,345 passed, 22 skipped with the same exclusion.
- Final sandbox suite: 385 passed, 22 skipped.
- Ruff lint, source/test formatting and git diff --check pass.
- Built wheel; extracted-wheel imports, scaffold generation and seccomp resource smoke passed outside the checkout.
- Commit parent/count and unchanged README verified; no push/PR performed.

Tests mirror modules under tests/sandbox/ and retain existing integration contracts. Direct private-helper tests were migrated to public observable contracts; repeated unconditional-build tests cover both engines, debug/normal and cache/no-cache flags with an absent Dockerfile.

### CI follow-up

- Updated .github/workflows/ci.yml to invoke tests/sandbox/test_launch_integration.py after the test-module move; verified all 16 integration tests collect with --integration.
- Corrected the system-target symlink test to compare its host source with Path('/usr/bin/env').resolve(), retaining '/usr/bin/env' as the container destination. On systems where env is itself a symlink (such as Rust coreutils packaging), expansion correctly emits the final host target; the old assertion was host-layout-dependent. Production mount logic is unchanged.
- Focused symlink suite: 40 passed; full available suite: 1,345 passed, 22 skipped with the same kubectl exclusion. Source/test lint, formatting and whitespace checks passed. Both fixes belong to the pure refactor; fast-startup is restacked above it.

### Rebase onto updated origin/main

Rebased onto dfbbabc after upstream merged recursive mount discovery, the Pi backend/migrations package split, expanded Pi state mounts, and per-model proxy endpoint routing. Resolved moves by preserving upstream implementations and migrating their imports/tests into sandbox modules. Migration/preparation now follows upstream ordering: build image, migrate_container_state(meta, runtime=...), on_before_container_start(meta, ..., runtime=...), then mounts/seeding. Retained all new migration failure/order tests and follow_links mode validation/rendering cases; removed the obsolete monolithic Pi test file in favor of upstream tests/agents/pi/.

- New-main Pi backend and symlink-expansion function/class declarations compare AST-identically after excluding documentation; only import destinations/logger identity are adapted.
- Full available pure-refactor suite: 1,447 passed, 46 skipped with the same kubectl exclusion. Focused sandbox/Pi/agent/session suite: 800 passed, 46 skipped. Integration target collects 16 tests; lint/format/whitespace checks pass.
- Earlier differential counts above describe the original 9d671d4 base. New-base validation is recorded here rather than presenting those older runs as tests of dfbbabc.
- Refactor remains one commit directly above origin/main; fast-startup is restacked separately. No push performed.

### Recursive alias test portability follow-up

CI exposed an inherited order-sensitive assertion in TestRecursiveLinks.test_aliases_are_scanned_at_each_container_location. os.walk preserves filesystem enumeration order for unrelated symlink branches; reversing that enumeration reproduced the failure locally with identical mount contents. Production traversal/order is unchanged. The test now asserts the original mount separately and compares the complete companion list sorted by destination, retaining duplicate detection and every mount field. Both forward and reverse filesystem enumeration are exercised via the real filesystem with an os.walk boundary wrapper.

- Symlink suite: 93 passed, 12 integration skips. Full available suite: 1,448 passed, 46 skipped with the documented kubectl exclusion. Lint/format/whitespace checks pass; remote CI still needs to run.
- Per user instruction, only sandbox-refactor is updated. Final review remains paused and fast-startup is not restacked until CI is green.

### Limits

This is strong bounded evidence, not a proof of universal equivalence. The full suite has a baseline native cryptography.x509 illegal-instruction crash in kubectl TLS setup; that module is excluded from comparative totals. Real engine integration previously encountered baseline missing-catatonit failures, and docker here is a Podman shim, not native Docker. The differential audit does not execute interactive PTY/sidecar launch, TTY build streaming, or exhaustive worktree/symlink/configuration matrices; corresponding source was compared and existing tests retained. Do not claim 100% certainty or native Docker execution coverage.

All implementation/review lanes are complete. Startup optimization remains explicitly deferred until after this pure refactor is accepted.
