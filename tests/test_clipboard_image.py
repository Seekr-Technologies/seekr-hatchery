"""Tests for the OSC 5522 paste interceptor."""

import base64
import time
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
# Osc5522Parser
# ---------------------------------------------------------------------------


def _frame(payload: bytes, *, meta: bytes = b"meta", terminator: bytes = ci.BEL) -> bytes:
    """Build a single OSC 5522 frame carrying *payload* (base64-encoded)."""
    return ci.OSC_5522_PREFIX + meta + b";" + base64.b64encode(payload) + terminator


class TestOsc5522Parser:
    def test_single_frame_decoded(self):
        p = ci.Osc5522Parser()
        residue = p.feed(_frame(b"hello world"))
        assert residue == b""
        assert bytes(p.payload) == b"hello world"
        assert p.frames_seen == 1
        # Not finished — terminator wasn't an empty-payload sentinel.
        assert p.finished is False

    def test_multiple_frames_concatenate(self):
        p = ci.Osc5522Parser()
        p.feed(_frame(b"abc") + _frame(b"def"))
        assert bytes(p.payload) == b"abcdef"
        assert p.frames_seen == 2

    def test_empty_frame_marks_finished(self):
        p = ci.Osc5522Parser()
        p.feed(_frame(b"abc"))
        p.feed(_frame(b""))
        assert bytes(p.payload) == b"abc"
        assert p.finished is True

    def test_st_terminator_accepted(self):
        p = ci.Osc5522Parser()
        p.feed(_frame(b"abc", terminator=ci.ST))
        assert bytes(p.payload) == b"abc"

    def test_residue_returned_before_and_after_frame(self):
        p = ci.Osc5522Parser()
        chunk = b"before " + _frame(b"img") + b"after"
        residue = p.feed(chunk)
        assert residue == b"before after"
        assert bytes(p.payload) == b"img"

    def test_chunk_split_mid_frame(self):
        p = ci.Osc5522Parser()
        full = _frame(b"split me")
        for ch in [full[: len(full) // 2], full[len(full) // 2 :]]:
            p.feed(ch)
        assert bytes(p.payload) == b"split me"
        assert p.frames_seen == 1

    def test_chunk_split_mid_prefix(self):
        p = ci.Osc5522Parser()
        full = _frame(b"X")
        # Split inside the OSC 5522 prefix.
        cut = len(ci.OSC_5522_PREFIX) - 2
        p.feed(full[:cut])
        p.feed(full[cut:])
        assert bytes(p.payload) == b"X"

    def test_junk_before_frame_is_residue(self):
        p = ci.Osc5522Parser()
        residue = p.feed(b"\x1b[Aregular keypress" + _frame(b"img"))
        # Cursor-up keypress + the rest should pass through as residue.
        assert b"\x1b[A" in residue
        assert b"regular keypress" in residue
        assert bytes(p.payload) == b"img"

    def test_undecodable_chunk_does_not_crash(self):
        p = ci.Osc5522Parser()
        # Inject raw bytes that aren't valid base64 inside a frame body.
        bad = ci.OSC_5522_PREFIX + b"meta;!!!not-base64!!!" + ci.BEL
        p.feed(bad)
        # Frame is counted but payload stays empty.
        assert p.frames_seen == 1


# ---------------------------------------------------------------------------
# BracketedPasteSplitter
# ---------------------------------------------------------------------------


class TestBracketedPasteSplitter:
    def test_plain_typing_passes_through(self):
        s = ci.BracketedPasteSplitter()
        assert s.feed(b"hello") == b"hello"
        assert s.completed_pastes == []

    def test_complete_paste_extracted(self):
        s = ci.BracketedPasteSplitter()
        chunk = b"prefix " + ci.BRACKETED_PASTE_START + b"PAYLOAD" + ci.BRACKETED_PASTE_END + b" tail"
        out = s.feed(chunk)
        assert out == b"prefix  tail"
        assert s.completed_pastes == [b"PAYLOAD"]

    def test_paste_split_across_feeds(self):
        s = ci.BracketedPasteSplitter()
        full = ci.BRACKETED_PASTE_START + b"hello" + ci.BRACKETED_PASTE_END
        cut = len(ci.BRACKETED_PASTE_START) + 2
        assert s.feed(full[:cut]) == b""
        assert s.completed_pastes == []
        assert s.feed(full[cut:]) == b""
        assert s.completed_pastes == [b"hello"]

    def test_start_marker_split(self):
        s = ci.BracketedPasteSplitter()
        full = b"AA" + ci.BRACKETED_PASTE_START + b"B" + ci.BRACKETED_PASTE_END
        cut = 2 + len(ci.BRACKETED_PASTE_START) - 2  # split inside start marker
        out1 = s.feed(full[:cut])
        out2 = s.feed(full[cut:])
        assert out1 + out2 == b"AA"
        assert s.completed_pastes == [b"B"]


# ---------------------------------------------------------------------------
# _looks_like_image_paste
# ---------------------------------------------------------------------------


class TestLooksLikeImagePaste:
    def test_empty_payload_is_image(self):
        assert ci._looks_like_image_paste(b"") is True

    def test_plain_text_is_not_image(self):
        assert ci._looks_like_image_paste(b"Hello, world!") is False

    def test_nul_byte_is_image(self):
        assert ci._looks_like_image_paste(b"\x00\x01\x02\x03\x04abc") is True

    def test_text_with_control_bytes_is_text(self):
        # NUL is the load-bearing signal; CR/LF in a multi-line paste is still text.
        assert ci._looks_like_image_paste(b"line1\nline2\r\nline3") is False


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
        import seekr_hatchery.clipboard_image as ci_mod

        class _FrozenDt:
            @staticmethod
            def now():
                class _T:
                    @staticmethod
                    def strftime(_fmt):
                        return "FROZEN"

                return _T()

        monkeypatch.setattr(ci_mod, "datetime", _FrozenDt)
        p1 = ci.save_image(b"\x89PNG\r\n\x1a\n" + b"a", tmp_path)
        p2 = ci.save_image(b"\x89PNG\r\n\x1a\n" + b"b", tmp_path)
        assert p1.name == "paste-FROZEN.png"
        assert p2.name == "paste-FROZEN-1.png"
        assert p1.read_bytes() != p2.read_bytes()


# ---------------------------------------------------------------------------
# PasteInterceptor — end-to-end byte flow
# ---------------------------------------------------------------------------


def _fmt(path: Path) -> str:
    return str(path)


class TestPasteInterceptorIdle:
    def test_plain_typing_forwards_unchanged(self, tmp_path):
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        result = pi.feed_stdin(b"hello\n")
        assert result.to_agent == b"hello\n"
        assert result.to_terminal == b""
        assert result.capture_started is False

    def test_text_paste_forwards_with_brackets(self, tmp_path):
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        chunk = ci.BRACKETED_PASTE_START + b"some text" + ci.BRACKETED_PASTE_END
        result = pi.feed_stdin(chunk)
        # Re-wraps so the agent sees bracketed paste boundaries it expects.
        assert result.to_agent == ci.BRACKETED_PASTE_START + b"some text" + ci.BRACKETED_PASTE_END
        assert result.to_terminal == b""
        assert result.capture_started is False
        assert pi.is_capturing() is False

    def test_image_paste_emits_query_and_swallows_payload(self, tmp_path):
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        chunk = ci.BRACKETED_PASTE_START + b"\x00\x01\x02bin" + ci.BRACKETED_PASTE_END
        result = pi.feed_stdin(chunk)
        assert result.to_agent == b""
        assert result.to_terminal == ci.OSC_5522_REQUEST
        assert result.capture_started is True
        assert pi.is_capturing() is True

    def test_empty_paste_triggers_query(self, tmp_path):
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        chunk = ci.BRACKETED_PASTE_START + ci.BRACKETED_PASTE_END
        result = pi.feed_stdin(chunk)
        assert result.capture_started is True
        assert result.to_terminal == ci.OSC_5522_REQUEST


class TestPasteInterceptorCapture:
    def _start_capture(self, tmp_path):
        pi = ci.PasteInterceptor(tmp_path, _fmt)
        pi.feed_stdin(ci.BRACKETED_PASTE_START + b"" + ci.BRACKETED_PASTE_END)
        assert pi.is_capturing()
        return pi

    def test_response_with_end_sentinel_writes_file(self, tmp_path):
        pi = self._start_capture(tmp_path)
        png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
        terminal_reply = _frame(png) + _frame(b"")  # data + empty-finish
        result = pi.feed_stdin(terminal_reply)
        # The injected path + trailing space lands in to_agent.
        assert result.to_agent.endswith(b" ")
        assert pi.is_capturing() is False
        # File was actually written.
        files = list(tmp_path.glob("paste-*.png"))
        assert len(files) == 1
        assert files[0].read_bytes() == png
        # And the injected path matches the saved file.
        assert str(files[0]).encode() in result.to_agent

    def test_user_typing_during_capture_is_passed_through(self, tmp_path):
        pi = self._start_capture(tmp_path)
        # Frames + user keystrokes interleaved.
        chunk = b"user1" + _frame(b"\x89PNG\r\n\x1a\nX") + b"user2" + _frame(b"")
        result = pi.feed_stdin(chunk)
        # Residue bytes (the keystrokes) reach the agent unchanged…
        assert b"user1" in result.to_agent
        assert b"user2" in result.to_agent
        # …followed by the injected path.
        assert b"paste-" in result.to_agent
        assert pi.is_capturing() is False

    def test_abort_capture_resets_state(self, tmp_path):
        pi = self._start_capture(tmp_path)
        pi.abort_capture()
        assert pi.is_capturing() is False
        # Subsequent paste can still trigger a fresh query.
        result = pi.feed_stdin(ci.BRACKETED_PASTE_START + b"" + ci.BRACKETED_PASTE_END)
        # But not after the terminal was marked unsupported — only one query
        # was actually attempted before abort, so the unsupported flag fires:
        assert result.capture_started is False
        assert result.to_terminal == b""

    def test_deadline_advances_with_chunks(self, tmp_path):
        pi = self._start_capture(tmp_path)
        before = pi.capture_deadline_at()
        # Feed a chunk that bumps frames_seen.
        pi.feed_stdin(_frame(b"\x89PNG\r\n\x1a\nA"))
        after = pi.capture_deadline_at()
        # Both deadlines are set, and after a chunk the deadline allows more time
        # (CHUNK_INACTIVITY_TIMEOUT_S > INITIAL_RESPONSE_TIMEOUT_S).
        assert before is not None and after is not None
        assert after >= before

    def test_aborting_with_no_frames_warns_once(self, tmp_path, capsys):
        pi = self._start_capture(tmp_path)
        pi.abort_capture()
        # Repeated abort of a fresh capture should not double-warn.
        pi.feed_stdin(ci.BRACKETED_PASTE_START + b"" + ci.BRACKETED_PASTE_END)
        # No second capture should have started since the terminal was tagged.
        assert pi.is_capturing() is False
