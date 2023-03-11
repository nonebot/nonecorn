from __future__ import annotations

import asyncio
import inspect
import os
import socket
import ssl
import sys
from enum import Enum
from functools import lru_cache
from importlib import import_module
from multiprocessing.synchronize import Event as EventType
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    cast,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

try:
    from uvloop import Loop
except ImportError:
    Loop = None

from .app_wrappers import ASGIWrapper, WSGIWrapper
from .config import Config
from .typing import AppWrapper, ASGIFramework, Framework, WSGIFramework

if TYPE_CHECKING:
    from .protocol.events import Request


class ShutdownError(Exception):
    pass


class NoAppError(Exception):
    pass


class LifespanTimeoutError(Exception):
    def __init__(self, stage: str) -> None:
        super().__init__(
            f"Timeout whilst awaiting {stage}. Your application may not support the ASGI Lifespan "
            f"protocol correctly, alternatively the {stage}_timeout configuration is incorrect."
        )


class LifespanFailureError(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"Lifespan failure in {stage}. '{message}'")


class UnexpectedMessageError(Exception):
    def __init__(self, state: Enum, message_type: str) -> None:
        super().__init__(f"Unexpected message type, {message_type} given the state {state}")


class FrameTooLargeError(Exception):
    pass


def suppress_body(method: str, status_code: int) -> bool:
    return method == "HEAD" or 100 <= status_code < 200 or status_code in {204, 304}


def build_and_validate_headers(headers: Iterable[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    # Validates that the header name and value are bytes
    validated_headers: List[Tuple[bytes, bytes]] = []
    for name, value in headers:
        if name[0] == b":"[0]:
            raise ValueError("Pseudo headers are not valid")
        validated_headers.append((bytes(name).strip(), bytes(value).strip()))
    return validated_headers


def filter_pseudo_headers(headers: List[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    filtered_headers: List[Tuple[bytes, bytes]] = [(b"host", b"")]  # Placeholder
    authority = None
    host = b""
    for name, value in headers:
        if name == b":authority":  # h2 & h3 libraries validate this is present
            authority = value
        elif name == b"host":
            host = value
        elif name[0] != b":"[0]:
            filtered_headers.append((name, value))
    filtered_headers[0] = (b"host", authority if authority is not None else host)
    return filtered_headers


def load_application(path: str, wsgi_max_body_size: int) -> AppWrapper:
    mode: Optional[Literal["asgi", "wsgi"]] = None
    if ":" not in path:
        module_name, app_name = path, "app"
    elif path.count(":") == 2:
        mode, module_name, app_name = path.split(":", 2)  # type: ignore
        if mode not in {"asgi", "wsgi"}:
            raise ValueError("Invalid mode, must be 'asgi', or 'wsgi'")
    else:
        module_name, app_name = path.split(":", 1)

    module_path = Path(module_name).resolve()
    sys.path.insert(0, str(module_path.parent))
    if module_path.is_file():
        import_name = module_path.with_suffix("").name
    else:
        import_name = module_path.name
    try:
        module = import_module(import_name)
    except ModuleNotFoundError as error:
        if error.name == import_name:
            raise NoAppError(f"Cannot load application from '{path}', module not found.")
        else:
            raise
    try:
        app = eval(app_name, vars(module))
    except NameError:
        raise NoAppError(f"Cannot load application from '{path}', application not found.")
    else:
        return wrap_app(app, wsgi_max_body_size, mode)


def wrap_app(
    app: Framework, wsgi_max_body_size: int, mode: Optional[Literal["asgi", "wsgi"]]
) -> AppWrapper:
    if mode is None:
        mode = "asgi" if is_asgi(app) else "wsgi"
    if mode == "asgi":
        return ASGIWrapper(cast(ASGIFramework, app))
    else:
        return WSGIWrapper(cast(WSGIFramework, app), wsgi_max_body_size)


def files_to_watch() -> Dict[Path, float]:
    last_updates: Dict[Path, float] = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        path = Path(filename)
        try:
            last_updates[Path(filename)] = path.stat().st_mtime
        except (FileNotFoundError, NotADirectoryError):
            pass
    return last_updates


def check_for_updates(files: Dict[Path, float]) -> bool:
    for path, last_mtime in files.items():
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return True
        else:
            if mtime > last_mtime:
                return True
            else:
                files[path] = mtime
    return False


async def raise_shutdown(shutdown_event: Callable[..., Awaitable]) -> None:
    await shutdown_event()
    raise ShutdownError()


async def check_multiprocess_shutdown_event(
    shutdown_event: EventType, sleep: Callable[[float], Awaitable[Any]]
) -> None:
    while True:
        if shutdown_event.is_set():
            return
        await sleep(0.1)


def write_pid_file(pid_path: str) -> None:
    with open(pid_path, "w") as file_:
        file_.write(f"{os.getpid()}")


def parse_socket_addr(family: int, address: tuple) -> Optional[Tuple[str, int]]:
    if family == socket.AF_INET:
        return address
    elif family == socket.AF_INET6:
        return (address[0], address[1])
    else:
        return None


def repr_socket_addr(family: int, address: tuple) -> str:
    if family == socket.AF_INET:
        return f"{address[0]}:{address[1]}"
    elif family == socket.AF_INET6:
        return f"[{address[0]}]:{address[1]}"
    elif family == socket.AF_UNIX:
        return f"unix:{address}"
    else:
        return f"{address}"


def valid_server_name(config: Config, request: "Request") -> bool:
    if len(config.server_names) == 0:
        return True

    host = ""
    for name, value in request.headers:
        if name.lower() == b"host":
            host = value.decode()
            break
    return host in config.server_names


RDNS_MAPPING: Dict[str, str] = {
    "commonName": "CN",
    "localityName": "L",
    "stateOrProvinceName": "ST",
    "organizationName": "O",
    "organizationalUnitName": "OU",
    "countryName": "C",
    "streetAddress": "STREET",
    "domainComponent": "DC",
    "userId": "UID",
}

TLS_VERSION_MAP: Dict[str, int] = {
    "TLSv1": 0x0301,
    "TLSv1.1": 0x0302,
    "TLSv1.2": 0x0303,
    "TLSv1.3": 0x0304,
}

TLS_CIPHER_SUITES: Final[dict[str, int]] = {
    "TLS_AEGIS_128L_SHA256": 4871,
    "TLS_AEGIS_256_SHA512": 4870,
    "TLS_AES_128_CCM_8_SHA256": 4869,
    "TLS_AES_128_CCM_SHA256": 4868,
    "TLS_AES_128_GCM_SHA256": 4865,
    "TLS_AES_256_GCM_SHA384": 4866,
    "TLS_CHACHA20_POLY1305_SHA256": 4867,
    "TLS_DHE_DSS_EXPORT_WITH_DES40_CBC_SHA": 17,
    "TLS_DHE_DSS_WITH_3DES_EDE_CBC_SHA": 19,
    "TLS_DHE_DSS_WITH_AES_128_CBC_SHA": 50,
    "TLS_DHE_DSS_WITH_AES_128_CBC_SHA256": 64,
    "TLS_DHE_DSS_WITH_AES_128_GCM_SHA256": 162,
    "TLS_DHE_DSS_WITH_AES_256_CBC_SHA": 56,
    "TLS_DHE_DSS_WITH_AES_256_CBC_SHA256": 106,
    "TLS_DHE_DSS_WITH_AES_256_GCM_SHA384": 163,
    "TLS_DHE_DSS_WITH_ARIA_128_CBC_SHA256": 49218,
    "TLS_DHE_DSS_WITH_ARIA_128_GCM_SHA256": 49238,
    "TLS_DHE_DSS_WITH_ARIA_256_CBC_SHA384": 49219,
    "TLS_DHE_DSS_WITH_ARIA_256_GCM_SHA384": 49239,
    "TLS_DHE_DSS_WITH_CAMELLIA_128_CBC_SHA": 68,
    "TLS_DHE_DSS_WITH_CAMELLIA_128_CBC_SHA256": 189,
    "TLS_DHE_DSS_WITH_CAMELLIA_128_GCM_SHA256": 49280,
    "TLS_DHE_DSS_WITH_CAMELLIA_256_CBC_SHA": 135,
    "TLS_DHE_DSS_WITH_CAMELLIA_256_CBC_SHA256": 195,
    "TLS_DHE_DSS_WITH_CAMELLIA_256_GCM_SHA384": 49281,
    "TLS_DHE_DSS_WITH_DES_CBC_SHA": 18,
    "TLS_DHE_DSS_WITH_SEED_CBC_SHA": 153,
    "TLS_DHE_PSK_WITH_3DES_EDE_CBC_SHA": 143,
    "TLS_DHE_PSK_WITH_AES_128_CBC_SHA": 144,
    "TLS_DHE_PSK_WITH_AES_128_CBC_SHA256": 178,
    "TLS_DHE_PSK_WITH_AES_128_CCM": 49318,
    "TLS_DHE_PSK_WITH_AES_128_GCM_SHA256": 170,
    "TLS_DHE_PSK_WITH_AES_256_CBC_SHA": 145,
    "TLS_DHE_PSK_WITH_AES_256_CBC_SHA384": 179,
    "TLS_DHE_PSK_WITH_AES_256_CCM": 49319,
    "TLS_DHE_PSK_WITH_AES_256_GCM_SHA384": 171,
    "TLS_DHE_PSK_WITH_ARIA_128_CBC_SHA256": 49254,
    "TLS_DHE_PSK_WITH_ARIA_128_GCM_SHA256": 49260,
    "TLS_DHE_PSK_WITH_ARIA_256_CBC_SHA384": 49255,
    "TLS_DHE_PSK_WITH_ARIA_256_GCM_SHA384": 49261,
    "TLS_DHE_PSK_WITH_CAMELLIA_128_CBC_SHA256": 49302,
    "TLS_DHE_PSK_WITH_CAMELLIA_128_GCM_SHA256": 49296,
    "TLS_DHE_PSK_WITH_CAMELLIA_256_CBC_SHA384": 49303,
    "TLS_DHE_PSK_WITH_CAMELLIA_256_GCM_SHA384": 49297,
    "TLS_DHE_PSK_WITH_CHACHA20_POLY1305_SHA256": 52397,
    "TLS_DHE_PSK_WITH_NULL_SHA": 45,
    "TLS_DHE_PSK_WITH_NULL_SHA256": 180,
    "TLS_DHE_PSK_WITH_NULL_SHA384": 181,
    "TLS_DHE_PSK_WITH_RC4_128_SHA": 142,
    "TLS_DHE_RSA_EXPORT_WITH_DES40_CBC_SHA": 20,
    "TLS_DHE_RSA_WITH_3DES_EDE_CBC_SHA": 22,
    "TLS_DHE_RSA_WITH_AES_128_CBC_SHA": 51,
    "TLS_DHE_RSA_WITH_AES_128_CBC_SHA256": 103,
    "TLS_DHE_RSA_WITH_AES_128_CCM": 49310,
    "TLS_DHE_RSA_WITH_AES_128_CCM_8": 49314,
    "TLS_DHE_RSA_WITH_AES_128_GCM_SHA256": 158,
    "TLS_DHE_RSA_WITH_AES_256_CBC_SHA": 57,
    "TLS_DHE_RSA_WITH_AES_256_CBC_SHA256": 107,
    "TLS_DHE_RSA_WITH_AES_256_CCM": 49311,
    "TLS_DHE_RSA_WITH_AES_256_CCM_8": 49315,
    "TLS_DHE_RSA_WITH_AES_256_GCM_SHA384": 159,
    "TLS_DHE_RSA_WITH_ARIA_128_CBC_SHA256": 49220,
    "TLS_DHE_RSA_WITH_ARIA_128_GCM_SHA256": 49234,
    "TLS_DHE_RSA_WITH_ARIA_256_CBC_SHA384": 49221,
    "TLS_DHE_RSA_WITH_ARIA_256_GCM_SHA384": 49235,
    "TLS_DHE_RSA_WITH_CAMELLIA_128_CBC_SHA": 69,
    "TLS_DHE_RSA_WITH_CAMELLIA_128_CBC_SHA256": 190,
    "TLS_DHE_RSA_WITH_CAMELLIA_128_GCM_SHA256": 49276,
    "TLS_DHE_RSA_WITH_CAMELLIA_256_CBC_SHA": 136,
    "TLS_DHE_RSA_WITH_CAMELLIA_256_CBC_SHA256": 196,
    "TLS_DHE_RSA_WITH_CAMELLIA_256_GCM_SHA384": 49277,
    "TLS_DHE_RSA_WITH_CHACHA20_POLY1305_SHA256": 52394,
    "TLS_DHE_RSA_WITH_DES_CBC_SHA": 21,
    "TLS_DHE_RSA_WITH_SEED_CBC_SHA": 154,
    "TLS_DH_DSS_EXPORT_WITH_DES40_CBC_SHA": 11,
    "TLS_DH_DSS_WITH_3DES_EDE_CBC_SHA": 13,
    "TLS_DH_DSS_WITH_AES_128_CBC_SHA": 48,
    "TLS_DH_DSS_WITH_AES_128_CBC_SHA256": 62,
    "TLS_DH_DSS_WITH_AES_128_GCM_SHA256": 164,
    "TLS_DH_DSS_WITH_AES_256_CBC_SHA": 54,
    "TLS_DH_DSS_WITH_AES_256_CBC_SHA256": 104,
    "TLS_DH_DSS_WITH_AES_256_GCM_SHA384": 165,
    "TLS_DH_DSS_WITH_ARIA_128_CBC_SHA256": 49214,
    "TLS_DH_DSS_WITH_ARIA_128_GCM_SHA256": 49240,
    "TLS_DH_DSS_WITH_ARIA_256_CBC_SHA384": 49215,
    "TLS_DH_DSS_WITH_ARIA_256_GCM_SHA384": 49241,
    "TLS_DH_DSS_WITH_CAMELLIA_128_CBC_SHA": 66,
    "TLS_DH_DSS_WITH_CAMELLIA_128_CBC_SHA256": 187,
    "TLS_DH_DSS_WITH_CAMELLIA_128_GCM_SHA256": 49282,
    "TLS_DH_DSS_WITH_CAMELLIA_256_CBC_SHA": 133,
    "TLS_DH_DSS_WITH_CAMELLIA_256_CBC_SHA256": 193,
    "TLS_DH_DSS_WITH_CAMELLIA_256_GCM_SHA384": 49283,
    "TLS_DH_DSS_WITH_DES_CBC_SHA": 12,
    "TLS_DH_DSS_WITH_SEED_CBC_SHA": 151,
    "TLS_DH_RSA_EXPORT_WITH_DES40_CBC_SHA": 14,
    "TLS_DH_RSA_WITH_3DES_EDE_CBC_SHA": 16,
    "TLS_DH_RSA_WITH_AES_128_CBC_SHA": 49,
    "TLS_DH_RSA_WITH_AES_128_CBC_SHA256": 63,
    "TLS_DH_RSA_WITH_AES_128_GCM_SHA256": 160,
    "TLS_DH_RSA_WITH_AES_256_CBC_SHA": 55,
    "TLS_DH_RSA_WITH_AES_256_CBC_SHA256": 105,
    "TLS_DH_RSA_WITH_AES_256_GCM_SHA384": 161,
    "TLS_DH_RSA_WITH_ARIA_128_CBC_SHA256": 49216,
    "TLS_DH_RSA_WITH_ARIA_128_GCM_SHA256": 49236,
    "TLS_DH_RSA_WITH_ARIA_256_CBC_SHA384": 49217,
    "TLS_DH_RSA_WITH_ARIA_256_GCM_SHA384": 49237,
    "TLS_DH_RSA_WITH_CAMELLIA_128_CBC_SHA": 67,
    "TLS_DH_RSA_WITH_CAMELLIA_128_CBC_SHA256": 188,
    "TLS_DH_RSA_WITH_CAMELLIA_128_GCM_SHA256": 49278,
    "TLS_DH_RSA_WITH_CAMELLIA_256_CBC_SHA": 134,
    "TLS_DH_RSA_WITH_CAMELLIA_256_CBC_SHA256": 194,
    "TLS_DH_RSA_WITH_CAMELLIA_256_GCM_SHA384": 49279,
    "TLS_DH_RSA_WITH_DES_CBC_SHA": 15,
    "TLS_DH_RSA_WITH_SEED_CBC_SHA": 152,
    "TLS_DH_anon_EXPORT_WITH_DES40_CBC_SHA": 25,
    "TLS_DH_anon_EXPORT_WITH_RC4_40_MD5": 23,
    "TLS_DH_anon_WITH_3DES_EDE_CBC_SHA": 27,
    "TLS_DH_anon_WITH_AES_128_CBC_SHA": 52,
    "TLS_DH_anon_WITH_AES_128_CBC_SHA256": 108,
    "TLS_DH_anon_WITH_AES_128_GCM_SHA256": 166,
    "TLS_DH_anon_WITH_AES_256_CBC_SHA": 58,
    "TLS_DH_anon_WITH_AES_256_CBC_SHA256": 109,
    "TLS_DH_anon_WITH_AES_256_GCM_SHA384": 167,
    "TLS_DH_anon_WITH_ARIA_128_CBC_SHA256": 49222,
    "TLS_DH_anon_WITH_ARIA_128_GCM_SHA256": 49242,
    "TLS_DH_anon_WITH_ARIA_256_CBC_SHA384": 49223,
    "TLS_DH_anon_WITH_ARIA_256_GCM_SHA384": 49243,
    "TLS_DH_anon_WITH_CAMELLIA_128_CBC_SHA": 70,
    "TLS_DH_anon_WITH_CAMELLIA_128_CBC_SHA256": 191,
    "TLS_DH_anon_WITH_CAMELLIA_128_GCM_SHA256": 49284,
    "TLS_DH_anon_WITH_CAMELLIA_256_CBC_SHA": 137,
    "TLS_DH_anon_WITH_CAMELLIA_256_CBC_SHA256": 197,
    "TLS_DH_anon_WITH_CAMELLIA_256_GCM_SHA384": 49285,
    "TLS_DH_anon_WITH_DES_CBC_SHA": 26,
    "TLS_DH_anon_WITH_RC4_128_MD5": 24,
    "TLS_DH_anon_WITH_SEED_CBC_SHA": 155,
    "TLS_ECCPWD_WITH_AES_128_CCM_SHA256": 49330,
    "TLS_ECCPWD_WITH_AES_128_GCM_SHA256": 49328,
    "TLS_ECCPWD_WITH_AES_256_CCM_SHA384": 49331,
    "TLS_ECCPWD_WITH_AES_256_GCM_SHA384": 49329,
    "TLS_ECDHE_ECDSA_WITH_3DES_EDE_CBC_SHA": 49160,
    "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA": 49161,
    "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA256": 49187,
    "TLS_ECDHE_ECDSA_WITH_AES_128_CCM": 49324,
    "TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8": 49326,
    "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256": 49195,
    "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA": 49162,
    "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA384": 49188,
    "TLS_ECDHE_ECDSA_WITH_AES_256_CCM": 49325,
    "TLS_ECDHE_ECDSA_WITH_AES_256_CCM_8": 49327,
    "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384": 49196,
    "TLS_ECDHE_ECDSA_WITH_ARIA_128_CBC_SHA256": 49224,
    "TLS_ECDHE_ECDSA_WITH_ARIA_128_GCM_SHA256": 49244,
    "TLS_ECDHE_ECDSA_WITH_ARIA_256_CBC_SHA384": 49225,
    "TLS_ECDHE_ECDSA_WITH_ARIA_256_GCM_SHA384": 49245,
    "TLS_ECDHE_ECDSA_WITH_CAMELLIA_128_CBC_SHA256": 49266,
    "TLS_ECDHE_ECDSA_WITH_CAMELLIA_128_GCM_SHA256": 49286,
    "TLS_ECDHE_ECDSA_WITH_CAMELLIA_256_CBC_SHA384": 49267,
    "TLS_ECDHE_ECDSA_WITH_CAMELLIA_256_GCM_SHA384": 49287,
    "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256": 52393,
    "TLS_ECDHE_ECDSA_WITH_NULL_SHA": 49158,
    "TLS_ECDHE_ECDSA_WITH_RC4_128_SHA": 49159,
    "TLS_ECDHE_PSK_WITH_3DES_EDE_CBC_SHA": 49204,
    "TLS_ECDHE_PSK_WITH_AES_128_CBC_SHA": 49205,
    "TLS_ECDHE_PSK_WITH_AES_128_CBC_SHA256": 49207,
    "TLS_ECDHE_PSK_WITH_AES_128_CCM_8_SHA256": 53251,
    "TLS_ECDHE_PSK_WITH_AES_128_CCM_SHA256": 53253,
    "TLS_ECDHE_PSK_WITH_AES_128_GCM_SHA256": 53249,
    "TLS_ECDHE_PSK_WITH_AES_256_CBC_SHA": 49206,
    "TLS_ECDHE_PSK_WITH_AES_256_CBC_SHA384": 49208,
    "TLS_ECDHE_PSK_WITH_AES_256_GCM_SHA384": 53250,
    "TLS_ECDHE_PSK_WITH_ARIA_128_CBC_SHA256": 49264,
    "TLS_ECDHE_PSK_WITH_ARIA_256_CBC_SHA384": 49265,
    "TLS_ECDHE_PSK_WITH_CAMELLIA_128_CBC_SHA256": 49306,
    "TLS_ECDHE_PSK_WITH_CAMELLIA_256_CBC_SHA384": 49307,
    "TLS_ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256": 52396,
    "TLS_ECDHE_PSK_WITH_NULL_SHA": 49209,
    "TLS_ECDHE_PSK_WITH_NULL_SHA256": 49210,
    "TLS_ECDHE_PSK_WITH_NULL_SHA384": 49211,
    "TLS_ECDHE_PSK_WITH_RC4_128_SHA": 49203,
    "TLS_ECDHE_RSA_WITH_3DES_EDE_CBC_SHA": 49170,
    "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA": 49171,
    "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA256": 49191,
    "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256": 49199,
    "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA": 49172,
    "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA384": 49192,
    "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384": 49200,
    "TLS_ECDHE_RSA_WITH_ARIA_128_CBC_SHA256": 49228,
    "TLS_ECDHE_RSA_WITH_ARIA_128_GCM_SHA256": 49248,
    "TLS_ECDHE_RSA_WITH_ARIA_256_CBC_SHA384": 49229,
    "TLS_ECDHE_RSA_WITH_ARIA_256_GCM_SHA384": 49249,
    "TLS_ECDHE_RSA_WITH_CAMELLIA_128_CBC_SHA256": 49270,
    "TLS_ECDHE_RSA_WITH_CAMELLIA_128_GCM_SHA256": 49290,
    "TLS_ECDHE_RSA_WITH_CAMELLIA_256_CBC_SHA384": 49271,
    "TLS_ECDHE_RSA_WITH_CAMELLIA_256_GCM_SHA384": 49291,
    "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256": 52392,
    "TLS_ECDHE_RSA_WITH_NULL_SHA": 49168,
    "TLS_ECDHE_RSA_WITH_RC4_128_SHA": 49169,
    "TLS_ECDH_ECDSA_WITH_3DES_EDE_CBC_SHA": 49155,
    "TLS_ECDH_ECDSA_WITH_AES_128_CBC_SHA": 49156,
    "TLS_ECDH_ECDSA_WITH_AES_128_CBC_SHA256": 49189,
    "TLS_ECDH_ECDSA_WITH_AES_128_GCM_SHA256": 49197,
    "TLS_ECDH_ECDSA_WITH_AES_256_CBC_SHA": 49157,
    "TLS_ECDH_ECDSA_WITH_AES_256_CBC_SHA384": 49190,
    "TLS_ECDH_ECDSA_WITH_AES_256_GCM_SHA384": 49198,
    "TLS_ECDH_ECDSA_WITH_ARIA_128_CBC_SHA256": 49226,
    "TLS_ECDH_ECDSA_WITH_ARIA_128_GCM_SHA256": 49246,
    "TLS_ECDH_ECDSA_WITH_ARIA_256_CBC_SHA384": 49227,
    "TLS_ECDH_ECDSA_WITH_ARIA_256_GCM_SHA384": 49247,
    "TLS_ECDH_ECDSA_WITH_CAMELLIA_128_CBC_SHA256": 49268,
    "TLS_ECDH_ECDSA_WITH_CAMELLIA_128_GCM_SHA256": 49288,
    "TLS_ECDH_ECDSA_WITH_CAMELLIA_256_CBC_SHA384": 49269,
    "TLS_ECDH_ECDSA_WITH_CAMELLIA_256_GCM_SHA384": 49289,
    "TLS_ECDH_ECDSA_WITH_NULL_SHA": 49153,
    "TLS_ECDH_ECDSA_WITH_RC4_128_SHA": 49154,
    "TLS_ECDH_RSA_WITH_3DES_EDE_CBC_SHA": 49165,
    "TLS_ECDH_RSA_WITH_AES_128_CBC_SHA": 49166,
    "TLS_ECDH_RSA_WITH_AES_128_CBC_SHA256": 49193,
    "TLS_ECDH_RSA_WITH_AES_128_GCM_SHA256": 49201,
    "TLS_ECDH_RSA_WITH_AES_256_CBC_SHA": 49167,
    "TLS_ECDH_RSA_WITH_AES_256_CBC_SHA384": 49194,
    "TLS_ECDH_RSA_WITH_AES_256_GCM_SHA384": 49202,
    "TLS_ECDH_RSA_WITH_ARIA_128_CBC_SHA256": 49230,
    "TLS_ECDH_RSA_WITH_ARIA_128_GCM_SHA256": 49250,
    "TLS_ECDH_RSA_WITH_ARIA_256_CBC_SHA384": 49231,
    "TLS_ECDH_RSA_WITH_ARIA_256_GCM_SHA384": 49251,
    "TLS_ECDH_RSA_WITH_CAMELLIA_128_CBC_SHA256": 49272,
    "TLS_ECDH_RSA_WITH_CAMELLIA_128_GCM_SHA256": 49292,
    "TLS_ECDH_RSA_WITH_CAMELLIA_256_CBC_SHA384": 49273,
    "TLS_ECDH_RSA_WITH_CAMELLIA_256_GCM_SHA384": 49293,
    "TLS_ECDH_RSA_WITH_NULL_SHA": 49163,
    "TLS_ECDH_RSA_WITH_RC4_128_SHA": 49164,
    "TLS_ECDH_anon_WITH_3DES_EDE_CBC_SHA": 49175,
    "TLS_ECDH_anon_WITH_AES_128_CBC_SHA": 49176,
    "TLS_ECDH_anon_WITH_AES_256_CBC_SHA": 49177,
    "TLS_ECDH_anon_WITH_NULL_SHA": 49173,
    "TLS_ECDH_anon_WITH_RC4_128_SHA": 49174,
    "TLS_EMPTY_RENEGOTIATION_INFO_SCSV": 255,
    "TLS_FALLBACK_SCSV": 22016,
    "TLS_GOSTR341112_256_WITH_28147_CNT_IMIT": 49410,
    "TLS_GOSTR341112_256_WITH_KUZNYECHIK_CTR_OMAC": 49408,
    "TLS_GOSTR341112_256_WITH_KUZNYECHIK_MGM_L": 49411,
    "TLS_GOSTR341112_256_WITH_KUZNYECHIK_MGM_S": 49413,
    "TLS_GOSTR341112_256_WITH_MAGMA_CTR_OMAC": 49409,
    "TLS_GOSTR341112_256_WITH_MAGMA_MGM_L": 49412,
    "TLS_GOSTR341112_256_WITH_MAGMA_MGM_S": 49414,
    "TLS_KRB5_EXPORT_WITH_DES_CBC_40_MD5": 41,
    "TLS_KRB5_EXPORT_WITH_DES_CBC_40_SHA": 38,
    "TLS_KRB5_EXPORT_WITH_RC2_CBC_40_MD5": 42,
    "TLS_KRB5_EXPORT_WITH_RC2_CBC_40_SHA": 39,
    "TLS_KRB5_EXPORT_WITH_RC4_40_MD5": 43,
    "TLS_KRB5_EXPORT_WITH_RC4_40_SHA": 40,
    "TLS_KRB5_WITH_3DES_EDE_CBC_MD5": 35,
    "TLS_KRB5_WITH_3DES_EDE_CBC_SHA": 31,
    "TLS_KRB5_WITH_DES_CBC_MD5": 34,
    "TLS_KRB5_WITH_DES_CBC_SHA": 30,
    "TLS_KRB5_WITH_IDEA_CBC_MD5": 37,
    "TLS_KRB5_WITH_IDEA_CBC_SHA": 33,
    "TLS_KRB5_WITH_RC4_128_MD5": 36,
    "TLS_KRB5_WITH_RC4_128_SHA": 32,
    "TLS_NULL_WITH_NULL_NULL": 0,
    "TLS_PSK_DHE_WITH_AES_128_CCM_8": 49322,
    "TLS_PSK_DHE_WITH_AES_256_CCM_8": 49323,
    "TLS_PSK_WITH_3DES_EDE_CBC_SHA": 139,
    "TLS_PSK_WITH_AES_128_CBC_SHA": 140,
    "TLS_PSK_WITH_AES_128_CBC_SHA256": 174,
    "TLS_PSK_WITH_AES_128_CCM": 49316,
    "TLS_PSK_WITH_AES_128_CCM_8": 49320,
    "TLS_PSK_WITH_AES_128_GCM_SHA256": 168,
    "TLS_PSK_WITH_AES_256_CBC_SHA": 141,
    "TLS_PSK_WITH_AES_256_CBC_SHA384": 175,
    "TLS_PSK_WITH_AES_256_CCM": 49317,
    "TLS_PSK_WITH_AES_256_CCM_8": 49321,
    "TLS_PSK_WITH_AES_256_GCM_SHA384": 169,
    "TLS_PSK_WITH_ARIA_128_CBC_SHA256": 49252,
    "TLS_PSK_WITH_ARIA_128_GCM_SHA256": 49258,
    "TLS_PSK_WITH_ARIA_256_CBC_SHA384": 49253,
    "TLS_PSK_WITH_ARIA_256_GCM_SHA384": 49259,
    "TLS_PSK_WITH_CAMELLIA_128_CBC_SHA256": 49300,
    "TLS_PSK_WITH_CAMELLIA_128_GCM_SHA256": 49294,
    "TLS_PSK_WITH_CAMELLIA_256_CBC_SHA384": 49301,
    "TLS_PSK_WITH_CAMELLIA_256_GCM_SHA384": 49295,
    "TLS_PSK_WITH_CHACHA20_POLY1305_SHA256": 52395,
    "TLS_PSK_WITH_NULL_SHA": 44,
    "TLS_PSK_WITH_NULL_SHA256": 176,
    "TLS_PSK_WITH_NULL_SHA384": 177,
    "TLS_PSK_WITH_RC4_128_SHA": 138,
    "TLS_RSA_EXPORT_WITH_DES40_CBC_SHA": 8,
    "TLS_RSA_EXPORT_WITH_RC2_CBC_40_MD5": 6,
    "TLS_RSA_EXPORT_WITH_RC4_40_MD5": 3,
    "TLS_RSA_PSK_WITH_3DES_EDE_CBC_SHA": 147,
    "TLS_RSA_PSK_WITH_AES_128_CBC_SHA": 148,
    "TLS_RSA_PSK_WITH_AES_128_CBC_SHA256": 182,
    "TLS_RSA_PSK_WITH_AES_128_GCM_SHA256": 172,
    "TLS_RSA_PSK_WITH_AES_256_CBC_SHA": 149,
    "TLS_RSA_PSK_WITH_AES_256_CBC_SHA384": 183,
    "TLS_RSA_PSK_WITH_AES_256_GCM_SHA384": 173,
    "TLS_RSA_PSK_WITH_ARIA_128_CBC_SHA256": 49256,
    "TLS_RSA_PSK_WITH_ARIA_128_GCM_SHA256": 49262,
    "TLS_RSA_PSK_WITH_ARIA_256_CBC_SHA384": 49257,
    "TLS_RSA_PSK_WITH_ARIA_256_GCM_SHA384": 49263,
    "TLS_RSA_PSK_WITH_CAMELLIA_128_CBC_SHA256": 49304,
    "TLS_RSA_PSK_WITH_CAMELLIA_128_GCM_SHA256": 49298,
    "TLS_RSA_PSK_WITH_CAMELLIA_256_CBC_SHA384": 49305,
    "TLS_RSA_PSK_WITH_CAMELLIA_256_GCM_SHA384": 49299,
    "TLS_RSA_PSK_WITH_CHACHA20_POLY1305_SHA256": 52398,
    "TLS_RSA_PSK_WITH_NULL_SHA": 46,
    "TLS_RSA_PSK_WITH_NULL_SHA256": 184,
    "TLS_RSA_PSK_WITH_NULL_SHA384": 185,
    "TLS_RSA_PSK_WITH_RC4_128_SHA": 146,
    "TLS_RSA_WITH_3DES_EDE_CBC_SHA": 10,
    "TLS_RSA_WITH_AES_128_CBC_SHA": 47,
    "TLS_RSA_WITH_AES_128_CBC_SHA256": 60,
    "TLS_RSA_WITH_AES_128_CCM": 49308,
    "TLS_RSA_WITH_AES_128_CCM_8": 49312,
    "TLS_RSA_WITH_AES_128_GCM_SHA256": 156,
    "TLS_RSA_WITH_AES_256_CBC_SHA": 53,
    "TLS_RSA_WITH_AES_256_CBC_SHA256": 61,
    "TLS_RSA_WITH_AES_256_CCM": 49309,
    "TLS_RSA_WITH_AES_256_CCM_8": 49313,
    "TLS_RSA_WITH_AES_256_GCM_SHA384": 157,
    "TLS_RSA_WITH_ARIA_128_CBC_SHA256": 49212,
    "TLS_RSA_WITH_ARIA_128_GCM_SHA256": 49232,
    "TLS_RSA_WITH_ARIA_256_CBC_SHA384": 49213,
    "TLS_RSA_WITH_ARIA_256_GCM_SHA384": 49233,
    "TLS_RSA_WITH_CAMELLIA_128_CBC_SHA": 65,
    "TLS_RSA_WITH_CAMELLIA_128_CBC_SHA256": 186,
    "TLS_RSA_WITH_CAMELLIA_128_GCM_SHA256": 49274,
    "TLS_RSA_WITH_CAMELLIA_256_CBC_SHA": 132,
    "TLS_RSA_WITH_CAMELLIA_256_CBC_SHA256": 192,
    "TLS_RSA_WITH_CAMELLIA_256_GCM_SHA384": 49275,
    "TLS_RSA_WITH_DES_CBC_SHA": 9,
    "TLS_RSA_WITH_IDEA_CBC_SHA": 7,
    "TLS_RSA_WITH_NULL_MD5": 1,
    "TLS_RSA_WITH_NULL_SHA": 2,
    "TLS_RSA_WITH_NULL_SHA256": 59,
    "TLS_RSA_WITH_RC4_128_MD5": 4,
    "TLS_RSA_WITH_RC4_128_SHA": 5,
    "TLS_RSA_WITH_SEED_CBC_SHA": 150,
    "TLS_SHA256_SHA256": 49332,
    "TLS_SHA384_SHA384": 49333,
    "TLS_SM4_CCM_SM3": 199,
    "TLS_SM4_GCM_SM3": 198,
    "TLS_SRP_SHA_DSS_WITH_3DES_EDE_CBC_SHA": 49180,
    "TLS_SRP_SHA_DSS_WITH_AES_128_CBC_SHA": 49183,
    "TLS_SRP_SHA_DSS_WITH_AES_256_CBC_SHA": 49186,
    "TLS_SRP_SHA_RSA_WITH_3DES_EDE_CBC_SHA": 49179,
    "TLS_SRP_SHA_RSA_WITH_AES_128_CBC_SHA": 49182,
    "TLS_SRP_SHA_RSA_WITH_AES_256_CBC_SHA": 49185,
    "TLS_SRP_SHA_WITH_3DES_EDE_CBC_SHA": 49178,
    "TLS_SRP_SHA_WITH_AES_128_CBC_SHA": 49181,
    "TLS_SRP_SHA_WITH_AES_256_CBC_SHA": 49184,
}

def escape_dn_chars(s: str) -> str:
    """
    Escape all DN special characters found in s
    with a back-slash (see RFC 4514, section 2.4)
    Based upon the implementation here - https://github.com/python-ldap/python-ldap/blob/e885b621562a3c987934be3fba3873d21026bf5c/Lib/ldap/dn.py#L17
    """
    if s:
        s = s.replace("\\", "\\\\")
        s = s.replace(",", "\\,")
        s = s.replace("+", "\\+")
        s = s.replace('"', '\\"')
        s = s.replace("<", "\\<")
        s = s.replace(">", "\\>")
        s = s.replace(";", "\\;")
        s = s.replace("=", "\\=")
        s = s.replace("\000", "\\\000")
        s = s.replace("\n", "\\0a")
        s = s.replace("\r", "\\0d")
        if s[-1] == " ":
            s = "".join((s[:-1], "\\ "))
        if s[0] == "#" or s[0] == " ":
            s = "".join(("\\", s))
    return s

def get_tls_info(ssl_object: ssl.SSLObject) -> Optional[Dict]:
    """
    # Copyed from https://github.com/encode/uvicorn/pull/1119. todo Let's see if it becomes the final solution
    # server_cert: Unable to set from transport information
    # client_cert_chain: Just the peercert, currently no access to the full cert chain
    # client_cert_name:
    # client_cert_error: No access to this
    # tls_version:
    # cipher_suite: Too hard to convert without direct access to openssl
    """
    ssl_info: Dict[str, Any] = {
        "server_cert": None,
        "client_cert_chain": [],
        "client_cert_name": None,
        "client_cert_error": None,
        "tls_version": None,
        "cipher_suite": None,
    }

    peercert = ssl_object.getpeercert()

    if peercert:
        rdn_strings = []
        for rdn in peercert["subject"]:
            rdn_strings.append(
                "+".join(
                    [
                        f"{RDNS_MAPPING[entry[0]]}={escape_dn_chars(entry[1])}"
                        for entry in reversed(rdn)
                        if entry[0] in RDNS_MAPPING
                    ]
                )
            )
        ssl_info["client_cert_chain"] = [
            ssl.DER_cert_to_PEM_cert(ssl_object.getpeercert(binary_form=True))
        ]
        ssl_info["client_cert_name"] = ", ".join(rdn_strings) if rdn_strings else ""
        ssl_info["tls_version"] = (
            TLS_VERSION_MAP[ssl_object.version()]
            if ssl_object.version() in TLS_VERSION_MAP
            else None
        )
        ssl_info["cipher_suite"] = TLS_CIPHER_SUITES[ssl_object.cipher()[0]]
        return ssl_info
    return None


def is_ssl(transport: asyncio.BaseTransport) -> bool:
    return bool(transport.get_extra_info("sslcontext"))


def check_uvloop(loop) -> bool:
    return isinstance(loop, Loop) if Loop else False


@lru_cache(2, typed=False)
def can_sendfile(loop: asyncio.AbstractEventLoop, https: bool = False) -> bool:
    """
    Judge loop.sendfile available. Uvloop not included.
    """
    return (
        sys.version_info[:2] >= (3, 7)
        and (
            (hasattr(asyncio, "ProactorEventLoop") and isinstance(loop, asyncio.ProactorEventLoop))
            or (isinstance(loop, asyncio.SelectorEventLoop) and hasattr(os, "sendfile"))
        )
        and not https
    )


def is_asgi(app: Any) -> bool:
    if inspect.iscoroutinefunction(app):
        return True
    elif hasattr(app, "__call__"):
        return inspect.iscoroutinefunction(app.__call__)
    return False
