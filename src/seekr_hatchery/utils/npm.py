"""npm registry helpers for pinning agent harnesses in Dockerfiles.

Stdlib-only (no local ``npm`` required): version lookups hit the registry's
HTTP API directly.
"""

import json
import re
import urllib.request


def npm_latest_version(pkg: str) -> str:
    """Return the latest published version of npm package *pkg*.

    Queries the npm registry's dist-tag endpoint over HTTPS so no local
    ``npm`` install is required on the host.
    """
    url = f"https://registry.npmjs.org/{pkg}/latest"
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
        return json.load(resp)["version"]


def current_npm_version(dockerfile_text: str, pkg: str) -> str | None:
    """Return the version *pkg* is currently pinned to in *dockerfile_text*.

    Returns ``None`` when the package appears unpinned (no ``@<ver>``) or is
    absent. The version group stops at whitespace or a line-continuation
    backslash, mirroring :func:`pin_npm_install`.
    """
    match = re.search(re.escape(pkg) + r"@([^\s\\]+)", dockerfile_text)
    return match.group(1) if match else None


def pin_npm_install(dockerfile_text: str, pkg: str, version: str) -> str:
    """Pin *pkg* to *version* in a ``npm install -g`` line of *dockerfile_text*.

    Rewrites both the unpinned (``<pkg>``) and already-pinned
    (``<pkg>@<ver>``) forms to ``<pkg>@<version>``. The optional trailing
    version group stops at whitespace or a line-continuation backslash.
    """
    pattern = re.escape(pkg) + r"(@[^\s\\]+)?"
    return re.sub(pattern, f"{pkg}@{version}", dockerfile_text)
