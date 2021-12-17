from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Iterable


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
    flush: bool = field(default_factory=lambda: False)


@dataclass(frozen=True)
class ZeroCopySend(Event):
    file: int
    offset: Optional[int] = None
    count: Optional[int] = None


@dataclass(frozen=True)
class TrailerHeadersSend(Event):
    headers: Optional[Iterable[Tuple[bytes, bytes]]] = field(default_factory=list)
    end_stream: bool = field(default_factory=lambda: True)


@dataclass(frozen=True)
class EndBody(Event):
    headers: Optional[Iterable[Tuple[bytes, bytes]]] = field(default_factory=list)


@dataclass(frozen=True)
class Data(Event):
    data: bytes
    flush: bool = field(default_factory=lambda: False)


@dataclass(frozen=True)
class EndData(Event):
    pass


@dataclass(frozen=True)
class Response(Event):
    headers: List[Tuple[bytes, bytes]]
    status_code: int
    http_version: str = field(default_factory=lambda: None)
    reason: str = field(default_factory=lambda: None)


@dataclass(frozen=True)
class StreamClosed(Event):
    pass
