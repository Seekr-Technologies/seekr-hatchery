"""Unit tests for the npm registry helpers."""

import textwrap

from seekr_hatchery.utils.npm import current_npm_version, pin_npm_install

CODEX = "@openai/codex"


def _dockerfile(install_line: str) -> str:
    """A realistic multi-line Dockerfile snippet wrapping *install_line*."""
    return textwrap.dedent(
        f"""\
        USER root
        RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \\
            && rm -rf /var/lib/apt/lists/*
        USER hatchery
        RUN npm config set prefix '/home/hatchery/.npm-global' \\
            && {install_line}
        """
    )


class TestCurrentNpmVersion:
    def test_returns_pinned_version(self):
        text = _dockerfile("npm install -g @openai/codex@0.1.0")
        assert current_npm_version(text, CODEX) == "0.1.0"

    def test_returns_none_when_unpinned(self):
        text = _dockerfile("npm install -g @openai/codex")
        assert current_npm_version(text, CODEX) is None

    def test_stops_at_line_continuation(self):
        text = _dockerfile("npm install -g @openai/codex@0.9.0 \\\n    && echo done")
        assert current_npm_version(text, CODEX) == "0.9.0"


class TestPinNpmInstall:
    def test_pins_unpinned_package(self):
        text = _dockerfile("npm install -g @openai/codex")
        assert current_npm_version(pin_npm_install(text, CODEX, "1.2.3"), CODEX) == "1.2.3"

    def test_repins_already_pinned_package(self):
        text = _dockerfile("npm install -g @openai/codex@0.1.0")
        assert current_npm_version(pin_npm_install(text, CODEX, "1.2.3"), CODEX) == "1.2.3"

    def test_preserves_surrounding_lines(self):
        text = _dockerfile("npm install -g --ignore-scripts @openai/codex \\\n    && npm cache clean --force")
        pinned = pin_npm_install(text, CODEX, "0.9.0")
        assert "npm install -g --ignore-scripts @openai/codex@0.9.0 \\" in pinned
        assert "&& npm cache clean --force" in pinned
        assert "&& rm -rf /var/lib/apt/lists/*" in pinned
