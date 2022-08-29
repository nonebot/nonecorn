from __future__ import annotations

import asyncio
import os
from ssl import SSLError
from typing import Any, Generator, IO, Optional

from .task_group import TaskGroup
from .worker_context import WorkerContext
from ..config import Config
from ..events import Closed, Event, RawData, Updated, ZeroCopySend
from ..protocol import ProtocolWrapper
from ..typing import AppWrapper
from ..utils import can_sendfile, get_tls_info, is_ssl, parse_socket_addr

MAX_RECV = 2**16


class TCPServer:
    def __init__(
        self,
        app: AppWrapper,
        loop: asyncio.AbstractEventLoop,
        config: Config,
        context: WorkerContext,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.loop = loop
        self.protocol: ProtocolWrapper
        self.reader = reader
        self.writer = writer
        # Set the buffer to 0 to avoid the problem of sending file before headers.
        if can_sendfile(loop, is_ssl(writer.transport)):
            self.writer.transport.set_write_buffer_limits(0)
        self.send_lock = asyncio.Lock()
        self.idle_lock = asyncio.Lock()

        self._idle_handle: Optional[asyncio.Task] = None

    def __await__(self) -> Generator[Any, None, None]:
        return self.run().__await__()

    async def run(self) -> None:
        socket = self.writer.get_extra_info("socket")
        tls = None

        try:
            client = parse_socket_addr(socket.family, socket.getpeername())
            server = parse_socket_addr(socket.family, socket.getsockname())
            ssl_object = self.writer.get_extra_info("ssl_object")
            if ssl_object is not None:
                ssl = True
                alpn_protocol = ssl_object.selected_alpn_protocol()
                tls = get_tls_info(self.writer)
                if tls:
                    tls["server_cert"] = self.config.cert_pem
            else:
                ssl = False
                alpn_protocol = "http/1.1"

            async with TaskGroup(self.loop) as task_group:
                self.protocol = ProtocolWrapper(
                    self.app,
                    self.config,
                    self.context,
                    task_group,
                    ssl,
                    client,
                    server,
                    self.protocol_send,
                    alpn_protocol,
                    tls,
                )
                await self.protocol.initiate()
                await self._start_idle()
                await self._read_data()
        except OSError:
            pass
        finally:
            await self._close()

    async def protocol_send(self, event: Event) -> None:
        if isinstance(event, RawData):
            async with self.send_lock:
                try:
                    self.writer.write(event.data)
                    await self.writer.drain()
                except (ConnectionError, RuntimeError):
                    await self.protocol.handle(Closed())
        elif isinstance(event, ZeroCopySend):
            await self.writer.drain()
            await self.zerocopysend(event.file, event.offset, event.count)
        elif isinstance(event, Closed):
            await self._close()
            await self.protocol.handle(Closed())
        elif isinstance(event, Updated):
            if event.idle:
                await self._start_idle()
            else:
                await self._stop_idle()

    async def zerocopysend(
        self, file: IO[bytes], offset: Optional[int] = None, count: Optional[int] = None
    ) -> None:
        if offset is None:
            offset = os.lseek(file.fileno(), 0, os.SEEK_CUR)
        if count is None:
            count = os.stat(file.fileno()).st_size - offset
        try:
            await self.loop.sendfile(self.writer.transport, file, offset, count)
        except (NotImplementedError, AttributeError):  # for uvloop
            os.sendfile(
                self.writer.transport.get_extra_info("socket").fileno(),
                file.fileno(),
                offset,
                count,
            )

    async def _read_data(self) -> None:
        while not self.reader.at_eof():
            try:
                data = await asyncio.wait_for(self.reader.read(MAX_RECV), self.config.read_timeout)
            except (
                ConnectionError,
                OSError,
                asyncio.TimeoutError,
                TimeoutError,
                SSLError,
            ):
                await self.protocol.handle(Closed())
                break
            else:
                await self.protocol.handle(RawData(data))

    async def _close(self) -> None:
        try:
            self.writer.write_eof()
        except (NotImplementedError, OSError, RuntimeError):
            pass  # Likely SSL connection

        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass  # Already closed

        await self._stop_idle()

    async def _initiate_server_close(self) -> None:
        await self.protocol.handle(Closed())
        self.writer.close()

    async def _start_idle(self) -> None:
        async with self.idle_lock:
            if self._idle_handle is None:
                self._idle_handle = self.loop.create_task(self._run_idle())

    async def _stop_idle(self) -> None:
        async with self.idle_lock:
            if self._idle_handle is not None:
                self._idle_handle.cancel()
                try:
                    await self._idle_handle
                except asyncio.CancelledError:
                    pass
            self._idle_handle = None

    async def _run_idle(self) -> None:
        try:
            await asyncio.wait_for(self.context.terminated.wait(), self.config.keep_alive_timeout)
        except asyncio.TimeoutError:
            pass
        await asyncio.shield(self._initiate_server_close())
