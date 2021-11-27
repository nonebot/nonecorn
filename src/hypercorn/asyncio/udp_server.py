from __future__ import annotations

import os
import asyncio
from typing import Optional, Tuple, IO, TYPE_CHECKING

from .task_group import TaskGroup
from .worker_context import WorkerContext
from ..config import Config
from ..events import Closed, Event, RawData, ZeroCopySend
from ..typing import ASGIFramework
from ..utils import parse_socket_addr, can_sendfile, is_ssl

if TYPE_CHECKING:
    # h3/Quic is an optional part of Hypercorn
    from ..protocol.quic import QuicProtocol  # noqa: F401


class UDPServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        app: ASGIFramework,
        loop: asyncio.AbstractEventLoop,
        config: Config,
        context: WorkerContext,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.loop = loop
        self.protocol: "QuicProtocol"
        self.protocol_queue: asyncio.Queue = asyncio.Queue(10)
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore
        # h3/Quic is an optional part of Hypercorn
        from ..protocol.quic import QuicProtocol  # noqa: F811
        # Set the buffer to 0 to avoid the problem of sending file before headers.
        if can_sendfile(self.loop, is_ssl(transport)):
            transport.set_write_buffer_limits(0)
        self.transport = transport
        socket = self.transport.get_extra_info("socket")
        server = parse_socket_addr(socket.family, socket.getsockname())
        task_group = TaskGroup(self.loop)
        self.protocol = QuicProtocol(
            self.app, self.config, self.context, task_group, server, self.protocol_send
        )
        task_group.spawn(self._consume_events)

    def datagram_received(self, data: bytes, address: Tuple[bytes, str]) -> None:  # type: ignore
        try:
            self.protocol_queue.put_nowait(RawData(data=data, address=address))  # type: ignore
        except asyncio.QueueFull:
            pass  # Just throw the data away, is UDP

    async def protocol_send(self, event: Event) -> None:
        if isinstance(event, RawData):
            self.transport.sendto(event.data, event.address)
        elif isinstance(event, ZeroCopySend):
            await self.zerocopysend(event.file, event.offset, event.count)

    async def zerocopysend(self, file: IO[bytes], offset: int = 0, count: Optional[int] = None) -> None:
        if offset is None:
            offset = os.lseek(file.fileno(), 0, os.SEEK_CUR)
        if count is None:
            count = os.stat(file.fileno()).st_size - offset
        try:
            await self.loop.sendfile(self.writer.transport, file, offset, count)
        except (NotImplementedError, AttributeError):
            os.sendfile(self.writer.transport.get_extra_info("socket").fileno(), file.fileno(), offset, count)

    async def _consume_events(self) -> None:
        while True:
            event = await self.protocol_queue.get()
            await self.protocol.handle(event)
            if isinstance(event, Closed):
                break
