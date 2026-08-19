"""Composable transforms over the PTY byte stream.

``StreamInterceptor`` is the per-direction transform protocol; ``InterceptorChain``
composes a sequence of them.  Concrete plugins live under ``interceptors/``.
"""

from seekr_hatchery.stream_interceptor.chain import InterceptorChain
from seekr_hatchery.stream_interceptor.protocol import StreamInterceptor

__all__ = ["InterceptorChain", "StreamInterceptor"]
