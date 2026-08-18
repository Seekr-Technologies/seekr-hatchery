"""Tests for the ``InterceptorChain`` composite.

These moved here from ``test_pty_proxy.py``: the per-direction fold and its
onion ordering now live in ``InterceptorChain``, so they are exercised
against the chain directly rather than through the PTY pump.
"""

from dataclasses import dataclass, field
from typing import Callable

from seekr_hatchery.stream_interceptor import InterceptorChain, StreamInterceptor


@dataclass
class _FakeInterceptor:
    """Configurable ``StreamInterceptor`` stand-in.

    Records every chunk it sees per direction and applies an optional
    transform; identity in both directions by default.
    """

    stdin_seen: list[bytes] = field(default_factory=list)
    stdout_seen: list[bytes] = field(default_factory=list)
    stdin_fn: Callable[[bytes], bytes] | None = None
    stdout_fn: Callable[[bytes], bytes] | None = None

    def on_stdin(self, chunk: bytes) -> bytes:
        self.stdin_seen.append(chunk)
        return self.stdin_fn(chunk) if self.stdin_fn else chunk

    def on_stdout(self, chunk: bytes) -> bytes:
        self.stdout_seen.append(chunk)
        return self.stdout_fn(chunk) if self.stdout_fn else chunk


class TestInterceptorChain:
    def test_stdin_folds_front_to_back(self):
        # First interceptor's output feeds the second — front-to-back order.
        first = _FakeInterceptor(stdin_fn=lambda c: c + b"-A")
        second = _FakeInterceptor()
        out = InterceptorChain([first, second]).on_stdin(b"x")
        assert first.stdin_seen == [b"x"]
        assert second.stdin_seen == [b"x-A"]
        assert out == b"x-A"

    def test_empty_list_is_identity_both_directions(self):
        chain = InterceptorChain([])
        assert chain.on_stdin(b"in") == b"in"
        assert chain.on_stdout(b"out") == b"out"

    def test_stdout_transform_is_applied(self):
        upper = _FakeInterceptor(stdout_fn=lambda c: c.upper())
        assert InterceptorChain([upper]).on_stdout(b"hello") == b"HELLO"

    def test_stdout_folds_back_to_front(self):
        # Reversed order for output: interceptors[-1] runs first, [0] last.
        a = _FakeInterceptor(stdout_fn=lambda c: c + b"|A")
        b = _FakeInterceptor(stdout_fn=lambda c: c + b"|B")
        assert InterceptorChain([a, b]).on_stdout(b"x") == b"x|B|A"

    def test_one_direction_plugin_uses_identity_default(self):
        # A plugin overriding only on_stdin inherits identity on_stdout from
        # the StreamInterceptor Protocol, so agent output is untouched.
        class _StdinOnly(StreamInterceptor):
            def on_stdin(self, chunk: bytes) -> bytes:
                return chunk.upper()

        chain = InterceptorChain([_StdinOnly()])
        assert chain.on_stdin(b"in") == b"IN"
        assert chain.on_stdout(b"agent-out") == b"agent-out"
