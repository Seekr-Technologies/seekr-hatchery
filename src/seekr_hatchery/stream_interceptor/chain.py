"""``InterceptorChain`` — a ``StreamInterceptor`` that composes a sequence of them."""

from typing import Sequence

from seekr_hatchery.stream_interceptor.protocol import StreamInterceptor


class InterceptorChain:
    """Fold a sequence of interceptors over the stream, per direction.

    Is itself a :class:`StreamInterceptor` (composite pattern), so a chain
    can stand in anywhere one interceptor is expected — including nested in
    another chain.

    Ordering is an onion: ``on_stdin`` folds **front-to-back**
    (``interceptors[0]`` first) and ``on_stdout`` folds **back-to-front**
    (``reversed``), so ``interceptors[0]`` is the outermost layer — the first
    to see bytes arriving from the user and the last to touch bytes leaving
    for the terminal.  An empty sequence is an identity pass-through in both
    directions.
    """

    def __init__(self, interceptors: Sequence[StreamInterceptor]) -> None:
        self._interceptors = interceptors

    def on_stdin(self, chunk: bytes) -> bytes:
        data = chunk
        for interceptor in self._interceptors:
            data = interceptor.on_stdin(data)
        return data

    def on_stdout(self, chunk: bytes) -> bytes:
        data = chunk
        for interceptor in reversed(self._interceptors):
            data = interceptor.on_stdout(data)
        return data
