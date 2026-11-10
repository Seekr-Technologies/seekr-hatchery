# Task: fast-startup

**Status**: complete
**Branch**: fast-startup
**Created**: 2026-11-10

## Objective

Skip sandbox builds across new sessions when agent/Dockerfile inputs match, and visibly report sandbox reuse. Stack as one feat: commit above pure sandbox-refactor (744831c).

## Summary

### Decision

sandbox/images.py:build_image fingerprints agent identity and exact Dockerfile bytes using versioned SHA-256. The build context is empty, so these cover local inputs. Images carry io.seekr-hatchery.build-fingerprint.

The cache is content-addressed as hatchery/cache:<fingerprint>, shared across sessions and repositories. A matching label on that cache tag causes runtime tag <cache-image> <session-image>, not a build. Cache hits print:

    Using cached sandbox image '<session-image>'
      Sandbox image ready.

Both normal and DEBUG modes show this message. Original seekr_hatchery.docker diagnostic logger identity remains unchanged.

Missing, stale, unlabeled or uninspectable cache images build using the normal layer cache, with both cache and session tags attached by the same build command. --rebuild-sandbox/no_cache bypasses inspection and passes --no-cache, updating both tags. Failed retagging reports source/target and honors exit_on_error; unreadable Dockerfiles report their path with the same exit-or-return-false choice.

### Rationale and limits

The initial implementation inspected only per-session tags: resumes were faster but new sessions still invoked the builder. User feedback exposed that gap. The shared fingerprint tag fixes new-task/chat reuse without changing existing session naming or launch interfaces.

Images created before shared caching require a build to populate the shared cache. Remote base-image/package inputs are not fingerprinted; forced builds rerun installation steps but do not explicitly pull a newer base image. Cache tags retain images until removed/pruned. Reuse crosses repositories because no repository files enter the empty context.

### Files and history

- src/seekr_hatchery/sandbox/images.py: shared-cache inspection, retagging, dual-tag builds and visible reuse output.
- tests/sandbox/test_images.py: both runtimes, normal/DEBUG output, cross-session hits, missing/unlabeled/stale cache, Dockerfile edits, agent switches, forced rebuilds, inspect/tag errors and complete process arguments.
- README.md: shared-cache behavior, visible output and freshness limitations.
- This task record: feature outcome, separate from the pure-refactor ADR.

fast-startup contains exactly one feat: commit above sandbox-refactor (744831c); the pure refactor remains unchanged. Original pre-port fast-startup remains on local fast-startup-before-rebase (0f53bd1). No duplicate refactor-plan commit was replayed.

### Validation

- Image suite: 45 passed.
- Broad suite: 1,377 passed, 22 skipped, excluding tests/test_kubectl_sidecar.py due the documented baseline native cryptography crash.
- Ruff lint, source/test formatting and git diff --check pass.
- Real Podman smoke: built once with both tags, reused under a different session tag with inspect/tag only, verified identical image IDs and exact reuse output, forced a no-cache build, then reused the refreshed cache under a third session. Temporary tags were removed.
- Native Docker engine was not available; Docker CLI argv is covered by process-boundary tests. Existing environment limitations remain documented in the refactor record.
- No push performed.
