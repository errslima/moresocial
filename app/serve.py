"""Run the public app (port 8000) and the private internal API (port 8001) in one process.

The internal listener is reachable only on private Docker networks (connectors) and the
host loopback mapping 127.0.0.1:8773 (provisioner); Caddy proxies only the public port.
"""
from __future__ import annotations

import asyncio
import os

import uvicorn

from . import config


async def main() -> None:
    settings = config.get()
    from .internal import create_internal_app
    from .web import create_app
    public = uvicorn.Server(uvicorn.Config(create_app(), host='0.0.0.0', port=int(os.environ.get('PORT', '8000')),
                                           proxy_headers=True, forwarded_allow_ips=','.join(settings.trusted_proxy_ips),
                                           access_log=False, server_header=False))
    internal = uvicorn.Server(uvicorn.Config(create_internal_app(), host='0.0.0.0',
                                             port=int(os.environ.get('INTERNAL_PORT', '8001')), access_log=False,
                                             server_header=False))
    await asyncio.gather(public.serve(), internal.serve())


if __name__ == '__main__':
    asyncio.run(main())
