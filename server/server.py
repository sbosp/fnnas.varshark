"""RayShark 后端服务入口。

监听两处：
1. 主服务：统一网关 Unix socket（生产）或 TCP（本地调试）。
   用 geventwebsocket 的 WebSocketHandler，支持 /api/ws。
2. Ingest 服务：本地回环 TCP（RAYSHARK_INGEST_PORT），
   专供 mitmdump addon 回传流量。mitm 是独立进程，只能走 HTTP。

两者共用同一个 Flask app（同一份内存 Hub/FlowBuffer），
因此 addon POST 进来的流量能实时广播给 ws 客户端。

运行模式：
- 生产：RAYSHARK_SOCK 指定 unix socket，网关反代。
- 调试：RAYSHARK_TCP=1 监听 127.0.0.1:PORT。
"""
import os
import sys
import logging

# gevent 必须最先 monkey patch
from gevent import monkey  # noqa: E402
monkey.patch_all()

import gevent  # noqa: E402
from gevent.pywsgi import WSGIServer  # noqa: E402
from geventwebsocket.handler import WebSocketHandler  # noqa: E402

from rayshark.app import create_app  # noqa: E402
from rayshark.config import Settings  # noqa: E402
from rayshark.procman import get_procman  # noqa: E402


def _setup_logging(logfile: str) -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        try:
            os.makedirs(os.path.dirname(logfile), exist_ok=True)
            handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
        except OSError:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _make_unix_listener(sock_path: str, log):
    import socket as _socket
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError as e:
        log.warning("failed to unlink stale socket: %s", e)
    os.makedirs(os.path.dirname(sock_path) or ".", exist_ok=True)
    listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    listener.setblocking(False)
    listener.bind(sock_path)
    listener.listen(256)
    try:
        os.chmod(sock_path, 0o666)
    except OSError as e:
        log.warning("chmod socket failed: %s", e)
    return listener


def main() -> int:
    settings = Settings.from_env()
    _setup_logging(settings.logfile)
    log = logging.getLogger("rayshark")
    log.info("=== RayShark server starting ===")
    log.info("webroot=%s prefix=%s", settings.webroot, settings.gateway_prefix)

    app = create_app(settings)

    # ---- 主服务器 ----
    if settings.tcp_mode:
        main_listener = (settings.tcp_host, settings.tcp_port)
        log.info("main listening on tcp %s:%s (debug)", *main_listener)
    else:
        main_listener = _make_unix_listener(settings.sock_path, log)
        log.info("main listening on unix socket %s", settings.sock_path)

    main_server = WSGIServer(main_listener, app, handler_class=WebSocketHandler,
                             log=log, error_log=log)

    # ---- Ingest 服务器（本地回环，mitm addon 回传）----
    ingest_server = WSGIServer(("127.0.0.1", settings.ingest_port), app,
                               log=None, error_log=log)
    log.info("ingest listening on tcp 127.0.0.1:%s", settings.ingest_port)

    def _shutdown(*_a):
        log.info("shutting down")
        try:
            get_procman().stop_all()
        except Exception as e:  # noqa: BLE001
            log.warning("stop_all failed: %s", e)
        main_server.stop()
        ingest_server.stop()

    gevent.signal_handler(2, _shutdown)   # SIGINT
    gevent.signal_handler(15, _shutdown)  # SIGTERM

    ingest_glet = gevent.spawn(ingest_server.serve_forever)
    try:
        main_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ingest_glet.kill()
        if not settings.tcp_mode:
            try:
                os.unlink(settings.sock_path)
            except OSError:
                pass
    log.info("server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
