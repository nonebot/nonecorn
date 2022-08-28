from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Event:
    stream_id: int


@dataclass(frozen=True)
class Request(Event):
    headers: List[Tuple[bytes, bytes]]
    http_version: str
    method: str
    raw_path: bytes


@dataclass(frozen=True)
class Body(Event):
    data: bytes
    flush: bool = False


@dataclass(frozen=True)
class ZeroCopySend(Event):
    file: int
    offset: Optional[int] = None
    count: Optional[int] = None


@dataclass(frozen=True)
class TrailerHeadersSend(Event):
    headers: Iterable[Tuple[bytes, bytes]] = field(default_factory=list)
    end_stream: bool = True


@dataclass(frozen=True)
class EndBody(Event):
    headers: Iterable[Tuple[bytes, bytes]] = field(default_factory=list)


@dataclass(frozen=True)
class Data(Event):
    data: bytes
    flush: bool = False


@dataclass(frozen=True)
class EndData(Event):
    pass


@dataclass(frozen=True)
class Response(Event):
    headers: List[Tuple[bytes, bytes]]
    status_code: int
    reason: str = ""


@dataclass(frozen=True)
class InformationalResponse(Event):
    headers: List[Tuple[bytes, bytes]]
    status_code: int

    def __post_init__(self) -> None:
        if self.status_code >= 200 or self.status_code < 100:
            raise ValueError(f"Status code must be 1XX not {self.status_code}")


@dataclass(frozen=True)
class StreamClosed(Event):
    pass
