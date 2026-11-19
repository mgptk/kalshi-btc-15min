"""WebSocket connect helper that works across websockets library versions.

websockets >= 13 ships the new asyncio client (`websockets.asyncio.client.connect`, which takes
`additional_headers`); older versions only have the legacy client (`extra_headers`). In 13.x the
top-level `websockets.connect` is still the legacy one, so pick the client explicitly.
"""
from __future__ import annotations


def ws_connect(url: str, headers: dict, **kwargs):
    try:
        from websockets.asyncio.client import connect  # websockets >= 13
    except ImportError:
        import websockets  # legacy client (< 13)

        return websockets.connect(url, extra_headers=headers, **kwargs)
    return connect(url, additional_headers=headers, **kwargs)
