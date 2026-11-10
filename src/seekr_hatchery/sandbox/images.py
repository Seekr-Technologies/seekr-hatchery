"""Sandbox image reuse, builds, and build-output streaming."""

import hashlib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from pathlib import Path

import click

import seekr_hatchery.agents as agent
import seekr_hatchery.sandbox.runtime as sandbox_runtime
import seekr_hatchery.sandbox.scaffold as scaffold
import seekr_hatchery.ui as ui

# Keep diagnostic output and logger-level selection stable across relocation.
logger = logging.getLogger("seekr_hatchery.docker")


_ANSI_RE = re.compile(r"\033\[[^a-zA-Z]*[a-zA-Z]|\r")
# Whitelist: only STEP/Step lines (both runtimes) and COMMIT lines (Podman).
# Filters out cache hits, layer hashes, "Successfully tagged", etc.
_BUILD_PROGRESS_RE = re.compile(r"^(STEP |COMMIT )", re.IGNORECASE)


def _stream_build(cmd: list[str], cwd: Path, n_lines: int = 4) -> tuple[int, list[str]]:
    """Run build cmd, showing a rolling n_lines buffer on TTY; silent capture otherwise.

    Returns (returncode, all_output_lines) — full output kept for error dumps.
    """
    if not sys.stdout.isatty():
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return result.returncode, (result.stderr or result.stdout).splitlines()

    term_w = shutil.get_terminal_size((80, 24)).columns
    buf: deque[str] = deque(maxlen=n_lines)
    all_lines: list[str] = []
    prev_n = 0

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for raw in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
        clean = _ANSI_RE.sub("", raw).strip()
        if not clean:
            continue
        all_lines.append(clean)
        if not _BUILD_PROGRESS_RE.match(clean):
            continue
        buf.append(clean[: term_w - 4])

        if prev_n:
            sys.stdout.write(f"\033[{prev_n}A")
        for line in buf:
            sys.stdout.write(f"\033[2K  {click.style(line, fg='blue')}\n")
        prev_n = len(buf)
        sys.stdout.flush()

    proc.wait()
    return proc.returncode, all_lines


_BUILD_FINGERPRINT_LABEL = "io.seekr-hatchery.build-fingerprint"


def build_image(
    repo: Path,
    hatchery_dir: Path,
    image_name: str,
    backend: agent.AgentBackend,
    runtime: sandbox_runtime.ContainerRuntime | None = None,
    no_cache: bool = False,
    exit_on_error: bool = True,
) -> bool:
    """Reuse a matching sandbox image, or build from Dockerfile.<agent>.

    A shared cache tag fingerprints the agent and Dockerfile bytes (the build
    context is empty). Matching cache images are tagged for the session without
    invoking the builder. ``no_cache`` bypasses reuse and the runtime layer
    cache, refreshing both tags; remote inputs are not fingerprinted.

    Using the hatchery_dir copy means Dockerfile changes made as part of a task
    are isolated to that task's image and merge into main with the task.

    Caller resolves *image_name* — typically ``sessions.image_name(repo, name)``.

    Returns True on a successful build or reuse. On failure the default is to print the
    error and ``sys.exit(1)``; pass ``exit_on_error=False`` to return False
    instead so the caller can recover (e.g. revert a Dockerfile edit).
    """
    runtime = runtime or sandbox_runtime.DockerRuntime()
    image = image_name
    worktree_dockerfile = scaffold.dockerfile_path(hatchery_dir, backend)
    try:
        dockerfile_bytes = worktree_dockerfile.read_bytes()
    except OSError as exc:
        ui.error(f"Cannot read Dockerfile: {worktree_dockerfile}\n  {exc}")
        if exit_on_error:
            sys.exit(1)
        return False

    # Version the fingerprint so future build-input changes can invalidate it.
    fingerprint = hashlib.sha256(b"v1\0" + backend.kind.encode() + b"\0" + dockerfile_bytes).hexdigest()
    cache_image = f"hatchery/cache:{fingerprint}"
    if not no_cache:
        inspected = subprocess.run(
            [
                runtime.binary,
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{_BUILD_FINGERPRINT_LABEL}" }}}}',
                cache_image,
            ],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if inspected.returncode == 0 and inspected.stdout.strip() == fingerprint:
            tagged = subprocess.run(
                [runtime.binary, "tag", cache_image, image],
                cwd=repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            if tagged.returncode != 0:
                ui.error(f"{runtime.binary} tag failed.\n  Source: {cache_image}\n  Target: {image}")
                if tagged.stderr.strip():
                    ui.info(tagged.stderr.strip())
                if exit_on_error:
                    sys.exit(1)
                return False
            logger.debug("Reusing %s image %r from %r", runtime.binary, image, cache_image)
            ui.info(click.style(f"Using cached sandbox image '{image}'", fg="magenta", bold=True))
            ui.success("  Sandbox image ready.")
            return True

    # Use a temporary empty directory as the build context — NOT the repo root.
    # The generated Dockerfile has no COPY/ADD from context (only multi-stage
    # COPY --from=), so an empty context is correct and avoids tar-ing the
    # entire repo (or .hatchery/worktrees/) which can hang indefinitely on
    # large repositories.
    with tempfile.TemporaryDirectory(prefix="hatchery-build-") as empty_context:
        build_cmd = [
            runtime.binary,
            "build",
            "-f",
            str(worktree_dockerfile),
            "-t",
            image,
            "-t",
            cache_image,
            "--label",
            f"{_BUILD_FINGERPRINT_LABEL}={fingerprint}",
        ]
        if no_cache:
            build_cmd.append("--no-cache")
        build_cmd.append(empty_context)
        logger.debug("Building %s image %r (context=%s)", runtime.binary, image, empty_context)
        logger.debug("Build command: %s", build_cmd)

        if logger.isEnabledFor(logging.DEBUG):
            # Let the runtime's own output pass through so build progress is visible.
            returncode = subprocess.run(build_cmd, cwd=repo, stdin=subprocess.DEVNULL).returncode
        else:
            ui.info(click.style(f"Building sandbox image '{image}'", fg="magenta", bold=True))
            returncode, output = _stream_build(build_cmd, cwd=repo)
            if returncode == 0:
                ui.success("  Image built.")
            else:
                for line in output[-20:]:
                    ui.info(f"  {line}")

        if returncode != 0:
            ui.error(f"{runtime.binary} build failed.\n  Dockerfile: {worktree_dockerfile}")
            if exit_on_error:
                sys.exit(1)
            return False
        return True
