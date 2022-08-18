from __future__ import annotations

from enum import auto, Enum
from time import time
from typing import Awaitable, Callable, Optional, Tuple
from urllib.parse import unquote

from .events import (
    DatagramBody,
    EndBody,
    EndData,
    Event,
    Request,
    Response,
    StreamClosed,
    WebTransportStreamBody,
)
from ..config import Config
from ..typing import (
    ASGIFramework,
    ASGISendEvent,
    TaskGroup,
    WebTransportAcceptEvent,
    WebTransportScope,
    WorkerContext,
)
from ..utils import UnexpectedMessageError, valid_server_name


class ASGIWebTransportState(Enum):
    HANDSHAKE = auto()
    CONNECTED = auto()
    CLOSED = auto()


class WebTransportStream:
    def __init__(
        self,
        app: ASGIFramework,
        config: Config,
        context: WorkerContext,
        task_group: TaskGroup,
        ssl: bool,
        client: Optional[Tuple[str, int]],
        server: Optional[Tuple[str, int]],
        send: Callable[[Event], Awaitable[None]],
        stream_id: int,
    ) -> None:
        self.app = app
        self.app_put: Optional[Callable] = None
        self.client = client
        self.closed = False
        self.config = config
        self.context = context
        self.task_group = task_group
        self.scope: WebTransportScope
        self.send = send
        # https://github.com/aiortc/aioquic/blob/c758b4d936b95347b46cc697da8d9823a3b059cc/examples/http3_server.py#L398
        self.scheme = "https" if ssl else "http"
        self.server = server
        self.start_time: float
        self.state = ASGIWebTransportState.HANDSHAKE
        self.stream_id = stream_id

    async def handle(self, event: Event) -> None:
        if self.closed:
            return
        elif isinstance(event, Request):
            self.start_time = time()
            path, _, query_string = event.raw_path.partition(b"?")
            self.scope = {
                "type": "webtransport",
                "asgi": {"spec_version": "2.3"},
                "scheme": self.scheme,
                "http_version": event.http_version,
                "path": unquote(path.decode("ascii")),
                "raw_path": path,
                "query_string": query_string,
                "root_path": self.config.root_path,
                "headers": event.headers,
                "client": self.client,
                "server": self.server,
            }

            if not valid_server_name(self.config, event):
                await self._send_error_response(404)
                self.closed = True
            else:
                self.app_put = await self.task_group.spawn_app(
                    self.app, self.config, self.scope, self.app_send
                )
                await self.app_put({"type": "webtransport.connect"})  # type: ignore
        elif isinstance(event, DatagramBody):
            await self.app_put(
                {
                    "type": "webtransport.datagram.receive",
                    "data": event.data,
                    "flow_id": event.flow_id,
                }
            )
        elif isinstance(event, WebTransportStreamBody):
            await self.app_put(
                {
                    "type": "webtransport.stream.receive",
                    "data": event.data,
                    "stream": event.stream_id,
                    "session": event.session_id,
                }
            )
        elif isinstance(event, (StreamClosed, EndBody)):
            self.closed = True
            if self.app_put is not None:
                await self.app_put({"type": "webtransport.close"})
        # todo handle events and webtransport.close? EndBody?

    async def app_send(self, message: Optional[ASGISendEvent]) -> None:
        if self.closed:
            # Allow app to finish after close
            return

        if message is None:  # ASGI App has finished sending messages
            # Cleanup if required
            await self.send(StreamClosed(stream_id=self.stream_id))
        else:
            if (
                message["type"] == "webtransport.accept"
                and self.state == ASGIWebTransportState.HANDSHAKE
            ):
                await self._accept(message)
            elif (
                message["type"] == "webtransport.datagram.send"
                and self.state == ASGIWebTransportState.CONNECTED
            ):
                await self.send(
                    DatagramBody(stream_id=1, flow_id=self.stream_id, data=message["data"])
                )
            elif (
                message["type"] == "webtransport.stream.send"
                and self.state == ASGIWebTransportState.CONNECTED
            ):
                await self.send(
                    WebTransportStreamBody(
                        stream_id=message["stream"], session_id=1, data=message["data"]
                    )
                )
            elif message["type"] == "webtransport.close":
                if self.state == ASGIWebTransportState.HANDSHAKE:
                    await self._send_error_response(403)
                else:
                    await self.send(EndData(stream_id=self.stream_id))
                self.state = ASGIWebTransportState.CLOSED
            else:
                raise UnexpectedMessageError(self.state, message["type"])
        # todo send events

    async def _accept(self, message: WebTransportAcceptEvent) -> None:
        self.state = ASGIWebTransportState.CONNECTED
        headers = message.get("headers", [])
        headers.append((b"sec-webtransport-http3-draft", b"draft02"))  # type: ignore
        await self.send(Response(stream_id=self.stream_id, status_code=200, headers=headers))  # type: ignore
        await self.config.log.access(
            self.scope, {"status": 200, "headers": []}, time() - self.start_time
        )

    async def _send_error_response(self, status_code: int) -> None:
        await self.send(
            Response(
                stream_id=self.stream_id,
                status_code=status_code,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
            )
        )
        await self.send(EndBody(stream_id=self.stream_id))
        await self.config.log.access(
            self.scope, {"status": status_code, "headers": []}, time() - self.start_time
        )
