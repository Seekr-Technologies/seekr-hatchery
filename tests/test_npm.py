"""Unit tests for the npm registry helpers."""

from seekr_hatchery.utils.npm import current_npm_version, pin_npm_install


class TestCurrentNpmVersion:
    def test_returns_pinned_version(self):
        line = "    && npm install -g @openai/codex@0.1.0"
        assert current_npm_version(line, "@openai/codex") == "0.1.0"

    def test_returns_none_when_unpinned(self):
        line = "    && npm install -g @openai/codex"
        assert current_npm_version(line, "@openai/codex") is None

    def test_stops_at_line_continuation(self):
        line = "RUN npm install -g @earendil-works/pi-coding-agent@0.9.0 \\"
        assert current_npm_version(line, "@earendil-works/pi-coding-agent") == "0.9.0"


class TestPinNpmInstall:
    def test_pins_unpinned_package(self):
        line = "    && npm install -g @openai/codex"
        assert pin_npm_install(line, "@openai/codex", "1.2.3") == "    && npm install -g @openai/codex@1.2.3"

    def test_repins_already_pinned_package(self):
        line = "    && npm install -g @openai/codex@0.1.0"
        assert pin_npm_install(line, "@openai/codex", "1.2.3") == "    && npm install -g @openai/codex@1.2.3"

    def test_preserves_surrounding_flags(self):
        line = "RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent \\"
        pkg = "@earendil-works/pi-coding-agent"
        expected = "RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.9.0 \\"
        assert pin_npm_install(line, pkg, "0.9.0") == expected
