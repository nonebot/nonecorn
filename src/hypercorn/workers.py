import asyncio
import signal
from functools import partial
from ssl import VerifyFlags, VerifyMode
from typing import Any, Awaitable, Callable, List

from gunicorn.config import (
    make_settings,
    Setting as GunicornSetting,
    validate_bool,
    validate_list_string,
    validate_pos_int,
    validate_string,
)
from gunicorn.sock import TCPSocket
from gunicorn.workers.base import Worker

from hypercorn.asyncio import serve as asyncio_serve
from hypercorn.config import Config as _Config, Sockets


class Config(_Config):
    sockets: Sockets = None

    def create_sockets(self) -> Sockets:
        return self.sockets


def transfer_sock(gunicorn_sock: List[TCPSocket]) -> Sockets:
    secure_sockets = []
    insecure_sockets = []
    for sock in gunicorn_sock:
        if sock.conf.is_ssl:
            secure_sockets.append(sock.sock)
        else:
            insecure_sockets.append(sock.sock)
    return Sockets(
        secure_sockets=secure_sockets, insecure_sockets=insecure_sockets, quic_sockets=[]
    )


def validate_pos_int_op(val):
    if val is None:
        return
    return validate_pos_int(val)


def validate_pos_float_op(val):
    if val is None:
        return
    val = float(val)
    if val < 0:
        raise ValueError("Value must be positive: %s" % val)
    return val


class alpn_protocols(GunicornSetting):
    name = "alpn_protocols"
    action = "append"
    section = "Server Mechanics"
    cli = ["--alpn_protocols"]
    meta = "CONFIG"
    validator = validate_list_string
    default = ["h2", "http/1.1"]
    desc = "alpn_protocols"


class alt_svc_headers(GunicornSetting):
    name = "alt_svc_headers"
    action = "append"
    section = "Server Mechanics"
    cli = ["--alt_svc_headers"]
    meta = "CONFIG"
    validator = validate_list_string
    default = []
    desc = "alt_svc_headers"


class debug(GunicornSetting):
    name = "debug"
    action = "store_true"
    section = "Server Mechanics"
    cli = ["--debug"]
    validator = validate_bool
    default = False
    desc = "debug"


class keep_alive_max_requests(GunicornSetting):
    name = "keep_alive_max_requests"
    section = "Worker Processes"
    cli = ["--keep_alive_max_requests"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "keep_alive_max_requests"


class read_timeout(GunicornSetting):
    name = "read_timeout"
    section = "Worker Processes"
    cli = ["--read_timeout"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "read_timeout"


class h11_max_incomplete_size(GunicornSetting):
    name = "h11_max_incomplete_size"
    section = "Server Mechanics"
    cli = ["--h11_max_incomplete_size"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "h11_max_incomplete_size"


class h11_pass_raw_headers(GunicornSetting):
    name = "h11_pass_raw_headers"
    section = "Server Mechanics"
    cli = ["--h11_pass_raw_headers"]
    action = "store_true"
    validator = validate_bool
    default = False
    desc = "debug"


class h2_max_concurrent_streams(GunicornSetting):
    name = "h2_max_concurrent_streams"
    section = "Server Mechanics"
    cli = ["--h2_max_concurrent_streams"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "h2_max_concurrent_streams"


class h2_max_header_list_size(GunicornSetting):
    name = "h2_max_header_list_size"
    section = "Server Mechanics"
    cli = ["--h2_max_header_list_size"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "h2_max_header_list_size"


class h2_max_inbound_frame_size(GunicornSetting):
    name = "h2_max_inbound_frame_size"
    section = "Server Mechanics"
    cli = ["--h2_max_inbound_frame_size"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "h2_max_inbound_frame_size"


class include_date_header(GunicornSetting):
    name = "include_date_header"
    section = "Server Mechanics"
    cli = ["--include_date_header"]
    action = "store_true"
    validator = validate_bool
    default = True
    desc = "include_date_header"


class include_server_header(GunicornSetting):
    name = "include_server_header"
    section = "Server Mechanics"
    cli = ["--include_server_header"]
    action = "store_true"
    validator = validate_bool
    default = True
    desc = "include_server_header"


class max_app_queue_size(GunicornSetting):
    name = "max_app_queue_size"
    section = "Server Mechanics"
    cli = ["--max_app_queue_size"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "max_app_queue_size"


class root_path(GunicornSetting):
    name = "root_path"
    section = "Server Mechanics"
    cli = ["--root_path"]
    meta = "STRING"
    validator = validate_string
    default = None
    desc = "root_path"


class server_names(GunicornSetting):
    name = "server_names"
    action = "append"
    section = "Server Mechanics"
    cli = ["--server_names"]
    meta = "CONFIG"
    validator = validate_list_string
    default = []
    desc = "server_names"


class shutdown_timeout(GunicornSetting):
    name = "shutdown_timeout"
    section = "Server Mechanics"
    cli = ["--shutdown_timeout"]
    validator = validate_pos_float_op
    default = None
    desc = "shutdown_timeout"


class ssl_handshake_timeout(GunicornSetting):
    name = "ssl_handshake_timeout"
    section = "Server Mechanics"
    cli = ["--ssl_handshake_timeout"]
    validator = validate_pos_float_op
    default = None
    desc = "ssl_handshake_timeout"


class startup_timeout(GunicornSetting):
    name = "startup_timeout"
    section = "Server Mechanics"
    cli = ["--startup_timeout"]
    validator = validate_pos_float_op
    default = None
    desc = "startup_timeout"


class verify_flags(GunicornSetting):
    name = "verify_flags"
    section = "Server Mechanics"
    cli = ["--verify_flags"]
    meta = "INT"
    validator = lambda v: VerifyFlags(validate_pos_int_op(v))
    default = None
    desc = "verify_flags"


class verify_mode(GunicornSetting):
    name = "verify_mode"
    section = "Server Mechanics"
    cli = ["--verify_mode"]
    meta = "INT"
    validator = lambda v: VerifyMode(validate_pos_int_op(v))
    default = None
    desc = "verify_mode"


class websocket_max_message_size(GunicornSetting):
    name = "websocket_max_message_size"
    section = "Server Mechanics"
    cli = ["--websocket_max_message_size"]
    meta = "INT"
    validator = validate_pos_int_op
    default = None
    desc = "websocket_max_message_size"


class websocket_ping_interval(GunicornSetting):
    name = "websocket_ping_interval"
    section = "Server Mechanics"
    cli = ["--websocket_ping_interval"]
    validator = validate_pos_float_op
    default = None
    desc = "websocket_ping_interval"


class wsgi_max_body_size(GunicornSetting):
    name = "wsgi_max_body_size"
    section = "Server Mechanics"
    cli = ["--wsgi_max_body_size"]
    validator = validate_pos_int_op
    default = None
    desc = "wsgi_max_body_size"


class HypercornAsyncioWorker(Worker):
    """
    Borrowed from uvicorn
    """

    CONFIG_KWARGS = {"worker_class": "asyncio"}

    def __init__(self, *args: Any, **kwargs: Any):
        super(HypercornAsyncioWorker, self).__init__(*args, **kwargs)
        self.cfg.settings = make_settings()  # 解除settings的限制
        self.app.load_config()  # 重新解析配置文件
        config_kwargs = {
            "access_log_format": self.cfg.access_log_format,
            "accesslog": self.cfg.accesslog,
            "alpn_protocols": self.cfg.alpn_protocols,
            "alt_svc_headers": self.cfg.alt_svc_headers,
            # "application_path": getattr(self.cfg, "application_path", None), suppressed by wsgi app
            "debug": getattr(self.cfg, "debug", None),
            "loglevel": self.cfg.loglevel.upper(),
            "errorlog": self.cfg.errorlog,
            "logconfig": self.cfg.logconfig,
            "keep_alive_timeout": self.cfg.keepalive,
            "keep_alive_max_requests": self.cfg.keep_alive_max_requests,
            "graceful_timeout": self.cfg.graceful_timeout,
            "read_timeout": self.cfg.read_timeout,
            "group": self.cfg.group,
            "dogstatsd_tags": self.cfg.dogstatsd_tags,
            "statsd_host": self.cfg.statsd_host,
            "statsd_prefix": self.cfg.statsd_prefix,
            "umask": self.cfg.umask,
            "user": self.cfg.user,
            "h11_max_incomplete_size": getattr(self.cfg, "h11_max_incomplete_size", None),
            "h11_pass_raw_headers": getattr(self.cfg, "h11_pass_raw_headers", None),
            "h2_max_concurrent_streams": getattr(self.cfg, "h2_max_concurrent_streams", None),
            "h2_max_header_list_size": getattr(self.cfg, "h2_max_header_list_size", None),
            "h2_max_inbound_frame_size": getattr(self.cfg, "h2_max_inbound_frame_size", None),
            "include_date_header": getattr(self.cfg, "include_date_header", None),
            "include_server_header": getattr(self.cfg, "include_server_header", None),
            "max_app_queue_size": getattr(self.cfg, "max_app_queue_size", None),
            "max_requests": self.cfg.max_requests or None,
            "max_requests_jitter": getattr(self.cfg, "max_requests_jitter", None),
            "pid_path": getattr(self.cfg, "pidfile", None),
            "root_path": getattr(self.cfg, "root_path", None),
            "server_names": getattr(self.cfg, "server_names", None),
            "shutdown_timeout": getattr(self.cfg, "shutdown_timeout", None),
            "ssl_handshake_timeout": getattr(self.cfg, "ssl_handshake_timeout", None),
            "startup_timeout": getattr(self.cfg, "startup_timeout", None),
            "verify_flags": getattr(self.cfg, "verify_flags", None),
            "verify_mode": getattr(self.cfg, "verify_mode", None),
            "websocket_max_message_size": getattr(self.cfg, "websocket_max_message_size", None),
            "websocket_ping_interval": getattr(self.cfg, "websocket_ping_interval", None),
            "wsgi_max_body_size": getattr(self.cfg, "wsgi_max_body_size", None),
        }
        config_kwargs.update(
            logconfig_dict=self.cfg.logconfig_dict if self.cfg.logconfig_dict else None
        )

        if self.cfg.is_ssl:
            ssl_kwargs = {
                "keyfile": self.cfg.ssl_options.get("keyfile"),
                "keyfile_password": self.cfg.ssl_options.get("password"),
                "certfile": self.cfg.ssl_options.get("certfile"),
                "ca_certs": self.cfg.ssl_options.get("ca_certs"),
                "cert_pem": getattr(self.cfg, "cert_pem", None),
            }
            if self.cfg.ssl_options.get("ciphers") is not None:
                ssl_kwargs.update(ciphers=self.cfg.ssl_options.get("ciphers"))
            config_kwargs.update(ssl_kwargs)

        if self.cfg.settings["backlog"].value:
            config_kwargs["backlog"] = self.cfg.settings["backlog"].value

        config_kwargs.update(self.CONFIG_KWARGS)
        self.config = Config()  # todo
        for k, v in config_kwargs.items():
            if v is not None:
                setattr(self.config, k, v)

    def init_signals(self):
        # Copy from uvicorn
        # Reset signals so Gunicorn doesn't swallow subprocess return codes
        # other signals are set up by Server.install_signal_handlers()
        # See: https://github.com/encode/uvicorn/issues/894
        for s in self.SIGNALS:
            signal.signal(s, signal.SIG_DFL)
        signal.signal(signal.SIGUSR1, self.handle_usr1)
        signal.siginterrupt(signal.SIGUSR1, False)

    def _install_sigquit_handler(self) -> None:
        """Install a SIGQUIT handler on workers.

        - https://github.com/encode/uvicorn/issues/1116
        - https://github.com/benoitc/gunicorn/issues/2604
        """
        # Copy from uvicorn
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGQUIT, self.handle_exit, signal.SIGQUIT, None)

    async def _asyncio_serve(self):
        self._install_sigquit_handler()
        await asyncio.wait(
            [
                asyncio.create_task(asyncio_serve(self.wsgi, self.config)),
                asyncio.create_task(self.asyncio_callback_notify()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _trio_serve(self):
        import trio

        from hypercorn.trio import serve as trio_serve

        async with trio.open_nursery() as nursery:

            async def wrap(func: Callable[[], Awaitable[Any]]) -> None:
                await func()
                nursery.cancel_scope.cancel()

            nursery.start_soon(wrap, partial(trio_serve, self.wsgi, self.config))
            await wrap(self.trio_callback_notify)

    def run(self):
        self.config.sockets = transfer_sock(
            self.sockets
        )  # patch hypercorn's socket, do not create, use gunicorn's
        if self.config.worker_class == "trio":
            import trio

            trio.run(self._trio_serve())
            return
        if self.config.worker_class == "uvloop":
            import uvloop

            uvloop.install()
        asyncio.run(self._asyncio_serve())

    async def asyncio_callback_notify(self):
        while True:
            self.notify()
            await asyncio.sleep(self.timeout)

    async def trio_callback_notify(self):
        import trio

        while True:
            self.notify()
            await trio.sleep(self.timeout)


class HypercornUvloopWorker(HypercornAsyncioWorker):
    CONFIG_KWARGS = {"worker_class": "uvloop"}


class HypercornTrioWorker(HypercornAsyncioWorker):
    CONFIG_KWARGS = {"worker_class": "trio"}
