"""Development/test stand-in for the production Caddy arrangement:

    redir /moresocial /moresocial/ 308
    handle_path /moresocial/* { reverse_proxy 127.0.0.1:8772 }

Requests outside the prefix get 404, exactly as on the shared host's fallback.
"""
from __future__ import annotations


class PrefixStrip:
    def __init__(self, app, prefix: str = '/moresocial'):
        self.app = app
        self.prefix = prefix.rstrip('/')

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        path = scope['path']
        if path == self.prefix:
            await send({'type': 'http.response.start', 'status': 308, 'headers': [(b'location', (self.prefix + '/').encode())]})
            await send({'type': 'http.response.body', 'body': b''})
            return
        if not path.startswith(self.prefix + '/'):
            await send({'type': 'http.response.start', 'status': 404, 'headers': [(b'content-type', b'text/plain')]})
            await send({'type': 'http.response.body', 'body': b'Not found'})
            return
        stripped = path[len(self.prefix):]
        raw = scope.get('raw_path')
        new = dict(scope, path=stripped, raw_path=raw[len(self.prefix):] if raw and raw.startswith(self.prefix.encode()) else raw)
        await self.app(new, receive, send)
