"""Clipboard image paste via OSC 5522 escape-sequence interception.

When the user pastes (Ctrl+V, Cmd+V) in a terminal with bracketed-paste
enabled, the terminal frames the payload with ``\\x1b[200~ ... \\x1b[201~``.
For ordinary text that's all we need — we just forward the bytes.  But
when the clipboard holds *image* data the terminal can't render it as
text and the paste is empty or garbled.  Kitty (and Ghostty's in-progress
implementation) expose the binary clipboard via an OSC 5522 query: we
write ``\\x1b]5522;type=image/png;encoding=base64\\x07`` to the terminal
and it streams the clipboard image back as one or more OSC 5522 frames
carrying base64 chunks.

This module is the protocol layer.  It is consumed by ``pty_proxy``,
which owns the actual file descriptors and select loop; we operate on
byte streams only so the whole thing is unit-testable without a real
PTY.

Wire format:
    Request:  \\x1b]5522;type=image/png;encoding=base64\\x07
    Response: \\x1b]5522;<meta>;<base64-chunk>\\x07  (one or many)
    Either BEL (``\\x07``) or ST (``\\x1b\\\\``) terminates a frame.
"""

import base64
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import seekr_hatchery.ui as ui

logger = logging.getLogger("hatchery")

# ── Protocol constants ────────────────────────────────────────────────────────

ESC: bytes = b"\x1b"
BEL: bytes = b"\x07"
ST: bytes = b"\x1b\\"

OSC_5522_PREFIX: bytes = b"\x1b]5522;"
# Request the clipboard image as base64-encoded PNG.  Trailing BEL terminator
# is universally accepted; ST works in stricter terminals but the BEL form is
# simpler and shorter on the wire.
OSC_5522_REQUEST: bytes = b"\x1b]5522;type=image/png;encoding=base64\x07"

BRACKETED_PASTE_START: bytes = b"\x1b[200~"
BRACKETED_PASTE_END: bytes = b"\x1b[201~"

# A clipboard response is given this much time to start arriving.  If
# nothing comes back within the budget the terminal is assumed not to
# speak OSC 5522 and we silently give up.
INITIAL_RESPONSE_TIMEOUT_S: float = 0.25
# Once a response *has* started, each additional chunk extends the deadline
# by this much.  Large screenshots (~5 MB) easily run past the initial
# budget but stream steadily.
CHUNK_INACTIVITY_TIMEOUT_S: float = 0.5
# Absolute ceiling on a single capture so a misbehaving terminal can't
# wedge the PTY loop forever.
HARD_CAPTURE_TIMEOUT_S: float = 5.0


# ── Magic-byte sniff ──────────────────────────────────────────────────────────

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def sniff_extension(data: bytes) -> str:
    """Return file extension for *data* based on a leading magic-byte sniff."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    # WebP has a split signature: "RIFF....WEBP" with size in between.
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "bin"


# ── Parser helpers ────────────────────────────────────────────────────────────


def _partial_match_len(data: bytes, marker: bytes) -> int:
    """Return the number of trailing bytes of *data* that match the start of *marker*.

    Used to decide how much tail to hold back when a chunk ends partway
    through a multi-byte escape sequence.  The check is a true suffix-
    vs-prefix test, so unrelated tail bytes never get held back.
    """
    max_match = min(len(marker) - 1, len(data))
    for k in range(max_match, 0, -1):
        if data.endswith(marker[:k]):
            return k
    return 0


# ── OSC 5522 parser ───────────────────────────────────────────────────────────


@dataclass
class Osc5522Parser:
    """Stream-oriented parser for OSC 5522 binary clipboard responses.

    Feed bytes incrementally with :meth:`feed`; recover the residue of
    bytes that did not belong to an OSC frame so the caller can forward
    them on as ordinary terminal input.  Decoded image bytes are
    accumulated in :attr:`payload`.

    A single instance handles exactly one capture.  Create a new
    instance per OSC 5522 query.
    """

    payload: bytearray = field(default_factory=bytearray)
    # Bytes carried over from the previous feed that may belong to an
    # OSC frame straddling reads (either a partial prefix outside a frame,
    # or partial in-frame body).
    _carry: bytearray = field(default_factory=bytearray)
    # True once we've matched the OSC 5522 prefix and are inside a frame.
    _in_frame: bool = False
    # True once a terminator has been seen following at least one frame
    # whose decoded payload was empty — Kitty uses an empty final chunk
    # to mark end-of-stream.
    finished: bool = False
    # Number of frames consumed (used by callers to verify the terminal
    # responded at all vs. silently ignoring the query).
    frames_seen: int = 0

    def feed(self, chunk: bytes) -> bytes:
        """Consume *chunk*; return any bytes that were NOT part of an OSC frame.

        Those residue bytes are typically ordinary user typing that
        arrived on stdin while we were waiting for the terminal's reply.
        The caller should forward them on to the child process.
        """
        residue = bytearray()
        data = bytes(self._carry) + chunk
        self._carry.clear()
        i = 0
        while i < len(data):
            if not self._in_frame:
                idx = data.find(OSC_5522_PREFIX, i)
                if idx < 0:
                    # No prefix found.  Hold back the tail that could be a
                    # partial prefix for next feed; emit the rest as residue.
                    keep = _partial_match_len(data[i:], OSC_5522_PREFIX)
                    cut = len(data) - keep
                    residue.extend(data[i:cut])
                    self._carry.extend(data[cut:])
                    return bytes(residue)
                # Forward everything before the prefix as residue.
                residue.extend(data[i:idx])
                i = idx + len(OSC_5522_PREFIX)
                self._in_frame = True
                continue
            # In-frame: accumulate until we see BEL or ST.
            bel_idx = data.find(BEL, i)
            st_idx = data.find(ST, i)
            candidates = [c for c in (bel_idx, st_idx) if c >= 0]
            if not candidates:
                # Frame body continues past this chunk's end.  Carry the in-progress
                # body plus a small tail margin so a split ST terminator survives.
                self._carry.extend(data[i:])
                return bytes(residue)
            term_idx = min(candidates)
            term_len = 2 if term_idx == st_idx and bel_idx != st_idx else 1
            self._consume_frame(data[i:term_idx])
            i = term_idx + term_len
            self._in_frame = False
        return bytes(residue)

    def _consume_frame(self, raw: bytes) -> None:
        """Decode an in-frame byte slice and append to :attr:`payload`."""
        self.frames_seen += 1
        # The frame body is "<meta>;<base64>"; the last ";" splits them.
        # When meta is absent (Kitty's terse responses) the body is pure base64.
        body = raw.split(b";", 1)[-1] if b";" in raw else raw
        body = body.strip()
        if not body:
            # Empty chunk = end-of-stream sentinel.
            self.finished = True
            return
        try:
            self.payload.extend(base64.b64decode(body, validate=False))
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            logger.debug("osc5522: discarding undecodable chunk of %d bytes", len(body))


# ── Bracketed-paste boundary detector ─────────────────────────────────────────


@dataclass
class BracketedPasteSplitter:
    """Split a byte stream into pass-through bytes and paste payloads.

    Feed bytes with :meth:`feed`; consume completed pastes from
    :attr:`completed_pastes` (a FIFO).  Bytes that are not inside a
    paste boundary are returned as the function's value.
    """

    _buf: bytearray = field(default_factory=bytearray)
    _in_paste: bool = False
    completed_pastes: list[bytes] = field(default_factory=list)

    def feed(self, chunk: bytes) -> bytes:
        out = bytearray()
        data = bytes(self._buf) + chunk
        self._buf.clear()
        i = 0
        while i < len(data):
            if not self._in_paste:
                idx = data.find(BRACKETED_PASTE_START, i)
                if idx < 0:
                    # Hold back only tail bytes that genuinely look like a
                    # partial start marker so plain typing flows through.
                    keep = _partial_match_len(data[i:], BRACKETED_PASTE_START)
                    cut = len(data) - keep
                    out.extend(data[i:cut])
                    self._buf.extend(data[cut:])
                    return bytes(out)
                out.extend(data[i:idx])
                i = idx + len(BRACKETED_PASTE_START)
                self._in_paste = True
                continue
            end = data.find(BRACKETED_PASTE_END, i)
            if end < 0:
                # Partial paste; stash the remainder for next feed.
                self._buf.extend(data[i:])
                return bytes(out)
            self.completed_pastes.append(bytes(data[i:end]))
            i = end + len(BRACKETED_PASTE_END)
            self._in_paste = False
        return bytes(out)


# ── File save ────────────────────────────────────────────────────────────────


def save_image(data: bytes, target_dir: Path) -> Path:
    """Write *data* to a uniquely-named file under *target_dir*.

    File name carries an ISO-ish timestamp so a series of pastes sort
    chronologically and so the user can tell at a glance which is which.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = sniff_extension(data)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    # Disambiguate within the same second.
    counter = 0
    while True:
        suffix = f"-{counter}" if counter else ""
        path = target_dir / f"paste-{stamp}{suffix}.{ext}"
        if not path.exists():
            break
        counter += 1
    path.write_bytes(data)
    return path


# ── Paste interceptor ─────────────────────────────────────────────────────────


def _looks_like_image_paste(payload: bytes) -> bool:
    """Heuristic: should this bracketed-paste payload trigger an OSC 5522 query?

    A normal text paste contains printable text.  Kitty and Ghostty deliver
    an image paste either as an empty bracketed-paste payload or as a
    payload of non-text bytes that wouldn't have come from a keyboard.

    Rule:
      - empty payload → treat as image (most reliable signal)
      - contains a NUL byte → treat as image (NUL never appears in text)
      - otherwise text
    """
    if not payload:
        return True
    return b"\x00" in payload


@dataclass
class PasteInputResult:
    """What ``PasteInterceptor.feed_stdin`` produced for the PTY proxy.

    Attributes:
        to_agent: bytes to write into the child's master PTY (forwarded
            stdin + possibly an injected file path after a successful
            capture).
        to_terminal: bytes to write back out to the user's stdout — the
            OSC 5522 query frame goes here when a paste fires it.
        capture_started: True iff an OSC 5522 query was issued in this
            feed.  The PTY proxy uses this to start a deadline timer.
    """

    to_agent: bytes = b""
    to_terminal: bytes = b""
    capture_started: bool = False


class PasteInterceptor:
    """High-level OSC-5522 paste glue used by ``pty_proxy``.

    Lifecycle (one per interactive session):

      1. ``feed_stdin(chunk)`` is called every time the user types or pastes.
         When a bracketed-paste boundary fires and the payload looks binary,
         the returned :class:`PasteInputResult` carries the OSC 5522 query
         in ``to_terminal``; the PTY proxy writes those bytes to the user's
         terminal and starts a deadline timer.

      2. ``feed_stdin_during_capture(chunk)`` is called once a capture has
         started.  It filters OSC 5522 frames out of the user's stdin and
         appends the decoded payload internally.

      3. When the capture finishes — either a frame announces end-of-stream
         or the PTY proxy reports the deadline expired — the proxy calls
         ``complete_capture()`` to commit the image to disk.  The returned
         bytes are typed back into the agent.

      4. On a captureless deadline the proxy calls ``abort_capture()`` and
         the interceptor stays usable for future pastes.
    """

    def __init__(
        self,
        target_dir: Path,
        format_image_reference: Callable[[Path], str],
    ) -> None:
        self._target_dir = target_dir
        self._format_image_reference = format_image_reference
        self._splitter = BracketedPasteSplitter()
        self._parser: Osc5522Parser | None = None
        self._capture_start: float | None = None
        self._terminal_unsupported = False

    # ── Public hooks ──────────────────────────────────────────────────────────

    def feed_stdin(self, chunk: bytes) -> PasteInputResult:
        """Process *chunk* from the user's stdin.

        Returns the bytes the PTY proxy should forward, alongside any
        side-channel writes (OSC query) and whether a capture started.
        """
        if self._parser is not None:
            return self._during_capture(chunk)
        return self._idle(chunk)

    def is_capturing(self) -> bool:
        return self._parser is not None

    def capture_deadline_at(self) -> float | None:
        """Return the wall-clock time at which the current capture should be aborted.

        ``None`` when no capture is active.  The PTY proxy polls this
        each iteration of its select loop and calls ``abort_capture`` or
        ``complete_capture`` as appropriate.
        """
        if self._parser is None or self._capture_start is None:
            return None
        # If we've seen no chunk yet, use the (shorter) initial timeout.
        budget = INITIAL_RESPONSE_TIMEOUT_S if self._parser.frames_seen == 0 else CHUNK_INACTIVITY_TIMEOUT_S
        # Also enforce the hard ceiling from the moment capture started.
        return min(
            self._capture_start + HARD_CAPTURE_TIMEOUT_S,
            time.monotonic() + budget,
        )

    def complete_capture(self) -> bytes:
        """Finalize an in-flight capture and return bytes to type into the agent.

        If the capture produced no data, returns ``b""`` and the
        interceptor reverts to idle.
        """
        parser = self._parser
        if parser is None:
            return b""
        self._parser = None
        self._capture_start = None
        if not parser.payload:
            return b""
        try:
            path = save_image(bytes(parser.payload), self._target_dir)
        except OSError as exc:
            logger.warning("Failed to save pasted image: %s", exc)
            return b""
        ref = self._format_image_reference(path)
        ui.info(f"📎 Pasted image saved as {path.name}")
        return ref.encode("utf-8") + b" "

    def abort_capture(self) -> None:
        """Drop the in-flight capture without writing a file."""
        parser = self._parser
        self._parser = None
        self._capture_start = None
        if parser is not None and parser.frames_seen == 0 and not self._terminal_unsupported:
            self._terminal_unsupported = True
            ui.warn(
                "Clipboard image paste is unavailable: this terminal didn't "
                "respond to OSC 5522.  Use a terminal that supports it "
                "(e.g. Kitty, Ghostty) or save the image to a file."
            )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _idle(self, chunk: bytes) -> PasteInputResult:
        passthrough = self._splitter.feed(chunk)
        # Drain completed pastes.  Multiple boundaries in a single chunk are
        # rare but possible; service them in arrival order.
        emitted_query = False
        agent_extra = bytearray()
        for payload in self._splitter.completed_pastes:
            if not self._terminal_unsupported and _looks_like_image_paste(payload):
                # Swallow the paste boundary entirely — we'll inject the path
                # after the OSC capture finishes.  Skip secondary queries while
                # we already have one in flight; the simplest policy is to
                # honour only the first.
                if not emitted_query:
                    emitted_query = True
                    self._parser = Osc5522Parser()
                    self._capture_start = time.monotonic()
                # else: silently drop additional simultaneous pastes.
            else:
                # Plain-text paste: re-wrap so the agent still sees a
                # bracketed-paste boundary (Codex/Claude Code use them).
                agent_extra.extend(BRACKETED_PASTE_START)
                agent_extra.extend(payload)
                agent_extra.extend(BRACKETED_PASTE_END)
        self._splitter.completed_pastes.clear()
        return PasteInputResult(
            to_agent=passthrough + bytes(agent_extra),
            to_terminal=OSC_5522_REQUEST if emitted_query else b"",
            capture_started=emitted_query,
        )

    def _during_capture(self, chunk: bytes) -> PasteInputResult:
        assert self._parser is not None
        residue = self._parser.feed(chunk)
        if self._parser.finished:
            injected = self.complete_capture()
            return PasteInputResult(to_agent=residue + injected, to_terminal=b"", capture_started=False)
        return PasteInputResult(to_agent=residue, to_terminal=b"", capture_started=False)
