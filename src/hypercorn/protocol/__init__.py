from __future__ import annotations

from typing import Awaitable, Callable, Optional, Tuple, Union, Dict, Any

from .h2 import H2Protocol
from .h11 import H2CProtocolRequiredError, H2ProtocolAssumedError, H11Protocol
from ..config import Config
from ..events import Event, RawData
from ..typing import AppWrapper, TaskGroup, WorkerContext


class ProtocolWrapper:
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
        alpn_protocol: Optional[str] = None,
        app_state: Dict[str, Any] = None,
    ) -> None:
        self.app = app
        self.config = config
        self.context = context
        self.task_group = task_group
        self.ssl = ssl
        self.client = client
        self.server = server
        self.send = send
        self.protocol: Union[H11Protocol, H2Protocol]
        self.app_state = app_state
        if alpn_protocol == "h2":
            self.protocol = H2Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.ssl,
                self.client,
                self.server,
                self.send,
                self.app_state,
            )
        else:
            self.protocol = H11Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.ssl,
                self.client,
                self.server,
                self.send,
                self.app_state,
            )

    async def initiate(self) -> None:
        return await self.protocol.initiate()

    async def handle(self, event: Event) -> None:
        try:
            return await self.protocol.handle(event)
        except H2ProtocolAssumedError as error:
            self.protocol = H2Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.ssl,
                self.client,
                self.server,
                self.send,
                self.app_state,
            )
            await self.protocol.initiate()
            if error.data != b"":
                return await self.protocol.handle(RawData(data=error.data))
        except H2CProtocolRequiredError as error:
            self.protocol = H2Protocol(
                self.app,
                self.config,
                self.context,
                self.task_group,
                self.ssl,
                self.client,
                self.server,
                self.send,
                self.app_state,
            )
            await self.protocol.initiate(error.headers, error.settings)
            if error.data != b"":
                return await self.protocol.handle(RawData(data=error.data))
