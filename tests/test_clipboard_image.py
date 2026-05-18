"""Tests for clipboard_image — Ctrl-V paste interception + host clipboard read."""

from pathlib import Path

import pytest

import seekr_hatchery.clipboard_image as ci

# ---------------------------------------------------------------------------
# sniff_extension
# ---------------------------------------------------------------------------


class TestSniffExtension:
    @pytest.mark.parametrize(
        "data, ext",
        [
            (b"\x89PNG\r\n\x1a\n" + b"...", "png"),
            (b"\xff\xd8\xff\xe0" + b"...", "jpg"),
            (b"GIF87a" + b"...", "gif"),
            (b"GIF89a" + b"...", "gif"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"...", "webp"),
            (b"random bytes here", "bin"),
            (b"", "bin"),
        ],
    )
    def test_extensions(self, data, ext):
        assert ci.sniff_extension(data) == ext


# ---------------------------------------------------------------------------
# save_image
# ---------------------------------------------------------------------------


class TestSaveImage:
    def test_writes_with_correct_extension(self, tmp_path):
        data = b"\x89PNG\r\n\x1a\n" + b"FAKE"
        path = ci.save_image(data, tmp_path)
        assert path.parent == tmp_path
        assert path.suffix == ".png"
        assert path.read_bytes() == data

    def test_creates_target_dir(self, tmp_path):
        target = tmp_path / "nested" / "dir"
        path = ci.save_image(b"GIF89a...", target)
        assert path.parent == target
        assert path.suffix == ".gif"

    def test_collisions_get_disambiguated(self, tmp_path, monkeypatch):
        # Freeze the timestamp portion so file names collide.
        class _FrozenDt:
            @staticmethod
            def now():
                class _T:
                    @staticmethod
                    def strftime(_fmt):
                        return "FROZEN"

                return _T()

        monkeypatch.setattr(ci, "datetime", _FrozenDt)
        p1 = ci.save_image(b"\x89PNG\r\n\x1a\n" + b"a", tmp_path)
        p2 = ci.save_image(b"\x89PNG\r\n\x1a\n" + b"b", tmp_path)
        assert p1.name == "paste-FROZEN.png"
        assert p2.name == "paste-FROZEN-1.png"
        assert p1.read_bytes() != p2.read_bytes()


# ---------------------------------------------------------------------------
# Host-clipboard read
# ---------------------------------------------------------------------------


_PNG = b"\x89PNG\r\n\x1a\nIMAGE-BYTES"


def _fmt(path: Path) -> str:
    return str(path)


class _Completed:
    def __init__(self, *, stdout, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class TestReadHostClipboardImage:
    def test_returns_none_when_platform_unsupported(self, monkeypatch):
        # Patch the attribute directly so the test survives a future
        # refactor that caches sys.platform at module load.
        monkeypatch.setattr(ci.sys, "platform", "win32")
        assert ci._read_host_clipboard_image() is None

    def test_darwin_dispatches_to_darwin_reader(self, monkeypatch):
        called = {}

        def _fake() -> bytes:
            called["yes"] = True
            return _PNG

        monkeypatch.setattr(ci.sys, "platform", "darwin")
        monkeypatch.setattr(ci, "_read_clipboard_darwin", _fake)
        assert ci._read_host_clipboard_image() == _PNG
        assert called == {"yes": True}

    def test_linux_dispatches_to_linux_reader(self, monkeypatch):
        monkeypatch.setattr(ci.sys, "platform", "linux")
        monkeypatch.setattr(ci, "_read_clipboard_linux", lambda: _PNG)
        assert ci._read_host_clipboard_image() == _PNG

    def test_darwin_returns_bytes_when_osascript_succeeds(self, monkeypatch, tmp_path):
        # Pretend osascript wrote a PNG to the NamedTemporaryFile path.
        target = tmp_path / "fake.png"
        target.write_bytes(_PNG)

        class _FakeTmp:
            name = str(target)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        monkeypatch.setattr(ci.tempfile, "NamedTemporaryFile", lambda **_: _FakeTmp())
        monkeypatch.setattr(
            ci.subprocess,
            "run",
            lambda *_a, **_k: _Completed(stdout="ok\n", returncode=0),
        )
        assert ci._read_clipboard_darwin() == _PNG
        # NamedTemporaryFile path is cleaned up by the reader.
        assert not target.exists()

    def test_darwin_returns_none_when_osascript_reports_no(self, monkeypatch, tmp_path):
        target = tmp_path / "fake.png"

        class _FakeTmp:
            name = str(target)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        monkeypatch.setattr(ci.tempfile, "NamedTemporaryFile", lambda **_: _FakeTmp())
        monkeypatch.setattr(
            ci.subprocess,
            "run",
            lambda *_a, **_k: _Completed(stdout="no\n", returncode=0),
        )
        assert ci._read_clipboard_darwin() is None

    def test_darwin_returns_none_when_osascript_missing(self, monkeypatch, tmp_path):
        target = tmp_path / "fake.png"

        class _FakeTmp:
            name = str(target)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def _raise(*_a, **_k):
            raise FileNotFoundError("osascript")

        monkeypatch.setattr(ci.tempfile, "NamedTemporaryFile", lambda **_: _FakeTmp())
        monkeypatch.setattr(ci.subprocess, "run", _raise)
        assert ci._read_clipboard_darwin() is None

    def test_linux_tries_wl_paste_then_xclip(self, monkeypatch):
        calls: list[list[str]] = []

        def _run(cmd, **_k):
            calls.append(cmd)
            if cmd[0] == "wl-paste":
                raise FileNotFoundError(cmd[0])
            return _Completed(stdout=_PNG, returncode=0)

        monkeypatch.setattr(ci.subprocess, "run", _run)
        assert ci._read_clipboard_linux() == _PNG
        assert calls[0][0] == "wl-paste"
        assert calls[1][0] == "xclip"

    def test_linux_returns_none_when_no_tool_available(self, monkeypatch):
        def _raise(cmd, **_k):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(ci.subprocess, "run", _raise)
        assert ci._read_clipboard_linux() is None


# ---------------------------------------------------------------------------
# PasteInterceptor — Ctrl-V (0x16) interception
# ---------------------------------------------------------------------------


class TestPasteInterceptor:
    def test_plain_typing_forwards_unchanged(self, tmp_path, monkeypatch):
        # Hot path: no Ctrl-V in the chunk, no subprocess work.
        called = []
        monkeypatch.setattr(
            ci,
            "_read_host_clipboard_image",
            lambda: called.append(1) or None,
        )
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"hello world\n")
        assert result.to_agent == b"hello world\n"
        assert called == []

    def test_ctrl_v_with_clipboard_image_injects_reference(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ci, "_read_host_clipboard_image", lambda: _PNG)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"\x16")
        # The 0x16 is swallowed and replaced with the saved-file reference.
        assert b"\x16" not in result.to_agent
        files = list(tmp_path.glob("paste-*.png"))
        assert len(files) == 1
        assert str(files[0]).encode() in result.to_agent
        assert result.to_agent.endswith(b" ")

    def test_ctrl_v_with_empty_clipboard_passes_through(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ci, "_read_host_clipboard_image", lambda: None)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"\x16")
        # No image to inject — preserve the original keystroke so it keeps
        # its normal meaning (vim visual-block, bash literal-insert, etc.).
        assert result.to_agent == b"\x16"

    def test_ctrl_v_in_middle_of_chunk_inserts_at_position(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ci, "_read_host_clipboard_image", lambda: _PNG)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"hi\x16there")
        # Replacement happens at the position of the 0x16 byte.
        files = list(tmp_path.glob("paste-*.png"))
        assert len(files) == 1
        ref = str(files[0]).encode()
        assert result.to_agent == b"hi" + ref + b" " + b"there"

    def test_multiple_ctrl_v_each_probe_clipboard(self, monkeypatch, tmp_path):
        # Each 0x16 in the chunk gets its own clipboard probe.  Useful if
        # the user rapidly fires two pastes (different images) into the
        # same stdin read.
        probes = []
        images = [_PNG, b"\x89PNG\r\n\x1a\nSECOND"]

        def _probe():
            probes.append(1)
            return images[len(probes) - 1] if len(probes) <= len(images) else None

        monkeypatch.setattr(ci, "_read_host_clipboard_image", _probe)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        pi.feed_stdin(b"\x16\x16")
        assert len(probes) == 2
        files = sorted(tmp_path.glob("paste-*.png"))
        assert len(files) == 2

    def test_kitty_ctrl_v_with_image_injects_reference(self, monkeypatch, tmp_path):
        # TUIs that enable the kitty keyboard protocol (CSI u) deliver
        # Ctrl-V as ESC[118;5u instead of raw 0x16.  Both forms must
        # trigger the same interception.
        monkeypatch.setattr(ci, "_read_host_clipboard_image", lambda: _PNG)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"\x1b[118;5u")
        assert b"\x1b[118;5u" not in result.to_agent
        files = list(tmp_path.glob("paste-*.png"))
        assert len(files) == 1
        assert str(files[0]).encode() in result.to_agent
        assert result.to_agent.endswith(b" ")

    def test_kitty_ctrl_v_with_empty_clipboard_passes_through(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ci, "_read_host_clipboard_image", lambda: None)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"\x1b[118;5u")
        # No image — preserve the whole sequence so it keeps whatever
        # meaning the agent assigns to it.
        assert result.to_agent == b"\x1b[118;5u"

    def test_kitty_ctrl_v_in_middle_of_chunk(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ci, "_read_host_clipboard_image", lambda: _PNG)
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"hi\x1b[118;5uthere")
        files = list(tmp_path.glob("paste-*.png"))
        assert len(files) == 1
        ref = str(files[0]).encode()
        assert result.to_agent == b"hi" + ref + b" " + b"there"

    def test_kitty_release_sequence_passes_through(self, monkeypatch, tmp_path):
        # The release event for V (ESC[118;1:3u — no Ctrl modifier) is a
        # different sequence and must NOT trigger a clipboard probe.
        called = []
        monkeypatch.setattr(
            ci,
            "_read_host_clipboard_image",
            lambda: called.append(1) or _PNG,
        )
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"\x1b[118;1:3u")
        assert result.to_agent == b"\x1b[118;1:3u"
        assert called == []
