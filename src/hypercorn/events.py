from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import IO, Optional, Tuple


class Event(ABC):
    pass


@dataclass(frozen=True)
class RawData(Event):
    data: bytes
    address: Optional[Tuple[str, int]] = None


@dataclass(frozen=True)
class ZeroCopySend(Event):
    file: IO[bytes]
    offset: Optional[int] = None
    count: Optional[int] = None


@dataclass(frozen=True)
class Closed(Event):
    pass


@dataclass(frozen=True)
class Updated(Event):
    idle: bool
