"""The ``StreamInterceptor`` protocol — a per-direction transform over PTY bytes."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class StreamInterceptor(Protocol):
    """A composable transform over the PTY byte stream, per direction.

    Each hook receives a chunk and returns the (possibly transformed) bytes
    to forward in that direction; an interceptor that has no work to do
    returns its input unchanged.  :class:`~seekr_hatchery.stream_interceptor.chain.InterceptorChain`
    folds a sequence of these per direction; ``pty_proxy`` drives a single one.

    The defaults are identity, so a plugin that cares about only one
    direction need override only that hook.  A plugin may either subclass
    this Protocol (inheriting the identity hooks) or satisfy it structurally
    by implementing both methods — see
    ``stream_interceptor.interceptors.clipboard_image.PasteInterceptor``.
    """

    def on_stdin(self, chunk: bytes) -> bytes:  # user → agent
        """Transform a stdin chunk on its way to the agent."""
        return chunk

    def on_stdout(self, chunk: bytes) -> bytes:  # agent → user
        """Transform a stdout chunk on its way to the user's terminal."""
        return chunk
