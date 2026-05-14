# Task: fix-kubectl

**Status**: complete
**Branch**: hatchery/fix-kubectl
**Created**: 2026-05-14 09:13

## Objective

The host-side kubectl RBAC proxy minted a self-signed TLS cert with a
hardcoded 24h validity and no refresh mechanism. Sandboxes that ran longer
than 24h hit `CERTIFICATE_VERIFY_FAILED` on every `kubectl` call and could
not recover without a `hatchery resume`. The reported second symptom (resume
doesn't fix it) turned out to be a misdiagnosis — resume does mint a fresh
cert as designed.

## Context

`_generate_self_signed_cert` (`kubectl_proxy.py`) hardcoded
`timedelta(days=1)`, the cert was minted once at sandbox launch in
`_kubectl_context()` (`docker.py`), the RBAC proxy lived for the container's
lifetime, and there was no background refresh.

## Summary

Bumped the cert's validity to 1 year (was 24h) and parameterized
`_generate_self_signed_cert(validity_days=365)`. Added one test that
locks the validity at ≥365 days so a future contributor doesn't
quietly revert it.

Why a long-lived self-signed cert is safe here: the cert is generated
in-memory, never persists past process exit, and is trusted only by
the per-task kubeconfig (which pins it as the sole CA and points at an
ephemeral `host.docker.internal:<port>` that's only reachable while
the proxy is alive). It can't be used off-host or after the proxy
dies, so cert rotation defends against no realistic threat. Don't
reintroduce short validity without an actual threat to point to —
the rationale lives in the function's docstring.

A first pass added a background refresh thread + `RBACProxy` class +
in-place kubeconfig rewrite with a load-bearing bind-mount-inode
constraint. All reverted; the simpler approach is the right one.

### Files

- `src/seekr_hatchery/kubectl_proxy.py` — `_generate_self_signed_cert`
  gains a `validity_days` parameter (default 365); docstring explains
  the threat-model reasoning.
- `tests/test_kubectl_proxy.py` — one new test under
  `TestCertGeneration` locking validity at ≥365 days.
