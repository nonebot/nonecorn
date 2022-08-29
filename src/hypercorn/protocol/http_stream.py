from __future__ import annotations

import asyncio
from enum import auto, Enum
from time import time
from typing import Awaitable, Callable, Optional, Tuple
from urllib.parse import unquote

from .events import (
    Body,
    EndBody,
    Event,
    InformationalResponse,
    Request,
    Response,
    StreamClosed,
    TrailerHeadersSend,
    ZeroCopySend,
)
from ..config import Config
from ..typing import (
    AppWrapper,
    ASGISendEvent,
    HTTPResponseStartEvent,
    HTTPScope,
    TaskGroup,
    WorkerContext,
)
from ..utils import (
    build_and_validate_headers,
    can_sendfile,
    suppress_body,
    UnexpectedMessageError,
    valid_server_name,
)

PUSH_VERSIONS = {"2", "3"}
EARLY_HINTS_VERSIONS = {"2", "3"}


class ASGIHTTPState(Enum):
    # The ASGI Spec is clear that a response should not start till the
    # framework has sent at least one body message hence why this
    # state tracking is required.
    REQUEST = auto()
    RESPONSE = auto()
    CLOSED = auto()


class HTTPStream:
    def __init__(
        self,
        app: AppWrapper,
        config: Config,
        context: WorkerContext,
        task_group: TaskGroup,
        ssl: bool,
        client: Optional[Tuple[str, int]],
        server: Optional[Tuple[str, int]],
        send: Callable[[Event], Awaitable[None]],
        stream_id: int,
        tls: Optional[dict] = None,
    ) -> None:
        self.app = app
        self.client = client
        self.closed = False
        self.config = config
        self.context = context
        self.response: HTTPResponseStartEvent
        self.scope: HTTPScope
        self.send = send
        self.scheme = "https" if ssl else "http"
        self.server = server
        self.start_time: float
        self.state = ASGIHTTPState.REQUEST
        self.stream_id = stream_id
        self.task_group = task_group
        self.tls = tls

    @property
    def idle(self) -> bool:
        return False

    async def handle(self, event: Event) -> None:
        if self.closed:
            return
        elif isinstance(event, Request):
            self.start_time = time()
            path, _, query_string = event.raw_path.partition(b"?")
            self.scope = {
                "type": "http",
                "http_version": event.http_version,
                "asgi": {"spec_version": "2.1", "version": "3.0"},
                "method": event.method,
                "scheme": self.scheme,
                "path": unquote(path.decode("ascii")),
                "raw_path": path,
                "query_string": query_string,
                "root_path": self.config.root_path,
                "headers": event.headers,
                "client": self.client,
                "server": self.server,
                "extensions": {},
            }
            self.scope["extensions"]["http.response.trailers"] = {}
            if event.http_version in PUSH_VERSIONS:
                self.scope["extensions"]["http.response.push"] = {}
            if (
                can_sendfile(asyncio.get_event_loop(), self.scheme == "https")
                and event.http_version not in PUSH_VERSIONS
            ):
                self.scope["extensions"]["http.response.zerocopysend"] = {}
            if self.scheme == "https" and self.tls:
                self.scope["extensions"]["tls"] = self.tls

            if event.http_version in EARLY_HINTS_VERSIONS:
                self.scope["extensions"]["http.response.early_hint"] = {}

            if valid_server_name(self.config, event):
                self.app_put = await self.task_group.spawn_app(
                    self.app, self.config, self.scope, self.app_send
                )
            else:
                await self._send_error_response(404)
                self.closed = True

        elif isinstance(event, Body):
            await self.app_put(
                {"type": "http.request", "body": bytes(event.data), "more_body": True}
            )
        elif isinstance(event, EndBody):
            await self.app_put({"type": "http.request", "body": b"", "more_body": False})
        elif isinstance(event, StreamClosed):
            self.closed = True
            if self.app_put is not None:
                await self.app_put({"type": "http.disconnect"})  # type: ignore

    async def app_send(self, message: Optional[ASGISendEvent]) -> None:
        if self.closed:
            # Allow app to finish after close
            return

        if message is None:  # ASGI App has finished sending messages
            # Cleanup if required
            if self.state == ASGIHTTPState.REQUEST:
                await self._send_error_response(500)
            await self.send(StreamClosed(stream_id=self.stream_id))
        else:
            if message["type"] == "http.response.start" and self.state == ASGIHTTPState.REQUEST:
                self.response = message
            elif (
                message["type"] == "http.response.push"
                and self.scope["http_version"] in PUSH_VERSIONS
            ):
                if not isinstance(message["path"], str):
                    raise TypeError(f"{message['path']} should be a str")
                headers = [(b":scheme", self.scope["scheme"].encode())]
                for name, value in self.scope["headers"]:
                    if name == b"host":
                        headers.append((b":authority", value))
                headers.extend(build_and_validate_headers(message["headers"]))
                await self.send(
                    Request(
                        stream_id=self.stream_id,
                        headers=headers,
                        http_version=self.scope["http_version"],
                        method="GET",
                        raw_path=message["path"].encode(),
                    )
                )
            elif (
                message["type"] == "http.response.early_hint"
                and self.scope["http_version"] in EARLY_HINTS_VERSIONS
                and self.state == ASGIHTTPState.REQUEST
            ):
                headers = [(b"link", bytes(link).strip()) for link in message["links"]]
                await self.send(
                    InformationalResponse(
                        stream_id=self.stream_id,
                        headers=headers,
                        status_code=103,
                    )
                )
            elif message["type"] == "http.response.body" and self.state in {
                ASGIHTTPState.REQUEST,
                ASGIHTTPState.RESPONSE,
            }:
                if self.state == ASGIHTTPState.REQUEST:
                    headers = build_and_validate_headers(self.response.get("headers", []))
                    reason = (
                        self.response["meta"].get("reason", "") if "meta" in self.response else ""
                    )
                    await self.send(
                        Response(
                            stream_id=self.stream_id,
                            headers=headers,
                            status_code=int(self.response["status"]),
                            reason=reason,
                        )
                    )
                    self.state = ASGIHTTPState.RESPONSE

                if (
                    not suppress_body(self.scope["method"], int(self.response["status"]))
                    and message.get("body", b"") != b""
                ):
                    await self.send(
                        Body(
                            stream_id=self.stream_id,
                            data=bytes(message.get("body", b"")),
                            flush=message["meta"].get("flush", False)
                            if "meta" in message
                            else False,
                        )
                    )

                if not message.get("more_body", False):
                    if self.state != ASGIHTTPState.CLOSED:
                        self.state = ASGIHTTPState.CLOSED
                        await self.config.log.access(
                            self.scope, self.response, time() - self.start_time
                        )
                        await self.send(
                            EndBody(
                                stream_id=self.stream_id,
                                headers=message["meta"].get("headers", [])
                                if "meta" in message
                                else [],
                            )
                        )
                        await self.send(StreamClosed(stream_id=self.stream_id))
            elif message["type"] == "http.response.zerocopysend" and self.state in {
                ASGIHTTPState.REQUEST,
                ASGIHTTPState.RESPONSE,
            }:
                if self.state == ASGIHTTPState.REQUEST:
                    headers = build_and_validate_headers(self.response.get("headers", []))
                    reason = (
                        self.response["meta"].get("reason", "") if "meta" in self.response else ""
                    )
                    await self.send(
                        Response(
                            stream_id=self.stream_id,
                            headers=headers,
                            status_code=int(self.response["status"]),
                            reason=reason,
                        )
                    )
                    self.state = ASGIHTTPState.RESPONSE

                if (
                    not suppress_body(self.scope["method"], int(self.response["status"]))
                    and message.get("file") is not None
                ):
                    await self.send(
                        ZeroCopySend(
                            stream_id=self.stream_id,
                            file=message.get("file"),
                            offset=message.get("offset"),
                            count=message.get("count"),
                        )
                    )

                if not message.get("more_body", False):
                    if self.state != ASGIHTTPState.CLOSED:
                        self.state = ASGIHTTPState.CLOSED
                        await self.config.log.access(
                            self.scope, self.response, time() - self.start_time
                        )
                        await self.send(
                            EndBody(
                                stream_id=self.stream_id,
                                headers=message["meta"].get("headers") if "meta" in message else [],
                            )
                        )
                        await self.send(StreamClosed(stream_id=self.stream_id))
            elif message["type"] == "http.response.trailers" and self.state in {
                ASGIHTTPState.REQUEST,
                ASGIHTTPState.RESPONSE,
            }:
                headers = message.get("headers", [])
                more_body = message.get("more_body", False)
                if not more_body:
                    await self.config.log.access(
                        self.scope, self.response, time() - self.start_time
                    )
                await self.send(
                    TrailerHeadersSend(
                        stream_id=self.stream_id, headers=headers, end_stream=not more_body
                    )
                )
                if not more_body:
                    if self.scope["http_version"] == "2":
                        await self.send(
                            EndBody(
                                stream_id=self.stream_id,
                                headers=[],
                            )
                        )
                    await self.send(StreamClosed(stream_id=self.stream_id))
            else:
                raise UnexpectedMessageError(self.state, message["type"])

    async def _send_error_response(self, status_code: int) -> None:
        await self.send(
            Response(
                stream_id=self.stream_id,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
                status_code=status_code,
            )
        )
        await self.send(EndBody(stream_id=self.stream_id))
        self.state = ASGIHTTPState.CLOSED
        await self.config.log.access(
            self.scope, {"status": status_code, "headers": []}, time() - self.start_time
        )
