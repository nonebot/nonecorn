from typing import List, Callable, Awaitable, Any
import asyncio
import signal
from functools import partial

from gunicorn.workers.base import Worker
from gunicorn.sock import TCPSocket

from hypercorn.config import Config as _Config, Sockets
from hypercorn.asyncio import serve as asyncio_serve


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
    return Sockets(secure_sockets=secure_sockets, insecure_sockets=insecure_sockets, quic_sockets=[])


class HypercornAsyncioWorker(Worker):
    """
    Borrowed from uvicorn
    """
    CONFIG_KWARGS = {"worker_class": "asyncio"}

    def __init__(self, *args, **kwargs):
        super(HypercornAsyncioWorker, self).__init__(*args, **kwargs)

        config_kwargs = {
            "access_log_format": self.cfg.access_log_format,
            "accesslog": self.cfg.accesslog,
            "alpn_protocols": self.cfg.alpn_protocols,
            "alt_svc_headers": self.cfg.alt_svc_headers,
            "debug": self.cfg.debug,
            "loglevel": self.cfg.loglevel.upper(),
            "errorlog": self.cfg.errorlog,
            "logconfig": self.cfg.logconfig,
            "keep_alive_timeout": self.cfg.keepalive,
            "graceful_timeout": self.cfg.graceful_timeout,
            "group": self.cfg.group,
            "dogstatsd_tags": self.cfg.dogstatsd_tags,
            "statsd_host": self.cfg.statsd_host,
            "statsd_prefix": self.cfg.statsd_prefix,
            "umask": self.cfg.umask,
            "user": self.cfg.user,
            "h11_max_incomplete_size": self.cfg.h11_max_incomplete_size,
            "h2_max_concurrent_streams": self.cfg.h2_max_concurrent_streams,
            "h2_max_header_list_size": self.cfg.h2_max_header_list_size,
            "h2_max_inbound_frame_size": self.cfg.h2_max_inbound_frame_size,
            "include_server_header": self.cfg.include_server_header,
            "logger_class": self.cfg.logger_class,
            "max_app_queue_size": self.cfg.max_app_queue_size,
            "pid_path": self.cfg.pid_path,
            "root_path": self.cfg.root_path,
            "server_names": self.cfg.server_names,
            "shutdown_timeout": self.cfg.shutdown_timeout,
            "ssl_handshake_timeout": self.cfg.ssl_handshake_timeout,
            "startup_timeout": self.cfg.startup_timeout,
            "verify_flags": self.cfg.verify_flags,
            "verify_mode": self.cfg.verify_mode,
            "websocket_max_message_size": self.cfg.websocket_max_message_size,
            "websocket_ping_interval": self.cfg.websocket_ping_interval,
        }
        config_kwargs.update(logconfig_dict=self.cfg.logconfig_dict if self.cfg.logconfig_dict else None)

        if self.cfg.is_ssl:
            ssl_kwargs = {
                "keyfile": self.cfg.ssl_options.get("keyfile"),
                "certfile": self.cfg.ssl_options.get("certfile"),
                "ca_certs": self.cfg.ssl_options.get("ca_certs"),
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
        for s in self.SIGNALS:
            signal.signal(s, signal.SIG_DFL)

    def run(self):
        asgi_app = self.wsgi
        self.config.sockets = transfer_sock(self.sockets)
        if self.config.worker_class == "trio":
            from hypercorn.trio import serve as trio_serve
            import trio

            async def start():
                async with trio.open_nursery() as nursery:
                    async def wrap(func: Callable[[], Awaitable[Any]]) -> None:
                        await func()
                        nursery.cancel_scope.cancel()

                    nursery.start_soon(wrap, partial(trio_serve, asgi_app, self.config))
                    await wrap(self.trio_callback_notify)

            trio.run(start)
            return
        if self.config.worker_class == "uvloop":
            import uvloop
            uvloop.install()
        asyncio.run(asyncio.wait([asyncio_serve(asgi_app, self.config),
                                  self.asyncio_callback_notify()],
                                 return_when=asyncio.FIRST_COMPLETED))

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
