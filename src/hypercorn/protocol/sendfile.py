import asyncio
import os
import sys

try:
    from uvloop import Loop
except ImportError:
    Loop = None


def is_ssl(transport: asyncio.Transport) -> bool:
    return bool(transport.get_extra_info("sslcontext"))


def check_uvloop(loop) -> bool:
    return isinstance(loop, Loop) if Loop else False


def can_sendfile(loop, https=False) -> bool:
    """
    Judge loop.sendfile available. Uvloop not included.
    """
    return sys.version_info[:2] >= (3, 7) and (
            (
                    hasattr(asyncio, "ProactorEventLoop")
                    and isinstance(loop, asyncio.ProactorEventLoop)
            )
            or (hasattr(os, "sendfile") and not (check_uvloop(loop) and https)))
