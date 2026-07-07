"""Flask 应用工厂。

- 静态托管前端 SPA
- REST API（/api/...）
- WebSocket（/api/ws）实时推流量与事件
- 鉴权：网关注入的 X-Trim-* 头（require_auth 开启时校验）
- 前缀剥离：网关转发完整路径（含 gatewayPrefix），应用自行 strip

模块初始化（db/procman/ws/proxy/capture）在 create_app 内完成。
"""
import logging
import os

from flask import Flask, g, jsonify, request, send_from_directory

from .config import Settings
from . import db as db_mod
from . import procman as pm_mod
from . import ws as ws_mod
from . import proxy as proxy_mod
from . import capture as cap_mod

log = logging.getLogger("rayshark.app")


class _StripPrefixMiddleware:
    """网关把完整路径(含 gatewayPrefix)转发到 socket，应用需自行剥离前缀。

    例：请求 /app/fnnas-rayshark/api/ping -> Flask 看到的 PATH_INFO = /api/ping
    """

    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and path.startswith(self.prefix):
            stripped = path[len(self.prefix):]
            if not stripped.startswith("/"):
                stripped = "/" + stripped
            environ["PATH_INFO"] = stripped
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
        return self.wsgi_app(environ, start_response)


class _WebSocketMiddleware:
    """在 WSGI 层直接处理 /api/ws，绕开 Flask dispatch。

    geventwebsocket 的 WebSocketHandler 完成 101 握手后，会用一个
    “吞掉响应”的 start_response 调用 application；这与 Flask 3 的
    正常 response 流程冲突，导致 view 不被执行。所以这里在中间件层
    拦截 ws 请求，拿到 environ['wsgi.websocket'] 后自行处理长连接，
    非 ws 请求原样透传给 Flask。

    需放在 StripPrefix 之内层：此时 PATH_INFO 已是 /api/ws。
    """

    def __init__(self, wsgi_app, ws_path: str = "/api/ws"):
        self.wsgi_app = wsgi_app
        self.ws_path = ws_path

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO") == self.ws_path:
            wsock = environ.get("wsgi.websocket")
            if wsock is not None:
                _serve_ws(wsock)
                return []
            # 不是有效的 ws 升级
            start_response("400 Bad Request", [("Content-Type", "application/json")])
            return [b'{"error":"expected websocket"}']
        return self.wsgi_app(environ, start_response)


def _serve_ws(wsock) -> None:
    """处理单个 ws 连接：注册 -> 补发历史 -> 保活 -> 注销。"""
    import json
    hub = ws_mod.get_hub()
    hub.register(wsock)
    try:
        for f in ws_mod.get_flows().snapshot(0):
            wsock.send(json.dumps({"type": "flow", "data": f}, ensure_ascii=False))
        wsock.send(json.dumps(
            {"type": "ready", "data": {"buffered": ws_mod.get_flows().count()}},
            ensure_ascii=False))
        while True:
            msg = wsock.receive()
            if msg is None:
                break
    except Exception as e:  # noqa: BLE001
        log.debug("ws closed: %s", e)
    finally:
        hub.unregister(wsock)


def _init_modules(settings: Settings) -> None:
    """初始化各单例模块。幂等，可安全多次调用。"""
    db_mod.init_db(settings.db_path)
    pm_mod.init_procman(settings.var_dir)
    ws_mod.init_ws(settings.flow_buffer_size)
    proxy_mod.init_proxy(settings.var_dir, settings.v2ray_bin)
    cap_mod.init_capture(
        settings.var_dir, settings.mitm_bin, settings.addon_path,
        settings.ingest_port, proxy_mod.TRANSPARENT_PORT,
    )
    log.info("modules initialized: db=%s v2ray_bin=%s mitm_bin=%s",
             settings.db_path, settings.v2ray_bin, settings.mitm_bin)


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["RAYSHARK"] = settings

    _init_modules(settings)
    _register_auth(app, settings)
    _register_api(app, settings)
    _register_static(app, settings)

    # 内层：ws 中间件（此时 PATH_INFO 仍是带前缀？否——strip 在更外层，
    # 所以这里包裹顺序为：请求 -> StripPrefix -> WebSocket -> Flask）
    app.wsgi_app = _WebSocketMiddleware(app.wsgi_app, ws_path="/api/ws")
    # 外层：先剥前缀，再进 ws 判断
    app.wsgi_app = _StripPrefixMiddleware(app.wsgi_app, settings.gateway_prefix)
    return app


def _register_auth(app: Flask, settings: Settings) -> None:
    @app.before_request
    def _auth():  # noqa: ANN202
        g.user_id = request.headers.get("X-Trim-Userid", "")
        g.username = request.headers.get("X-Trim-Username", "")
        g.is_admin = request.headers.get("X-Trim-Isadmin", "").lower() in ("1", "true")

        path = request.path
        # 放行：健康检查、内部回传、静态资源、WebSocket 握手
        if (path == "/api/ping" or path == "/api/ws"
                or path.startswith("/api/_ingest/")
                or not path.startswith("/api/")):
            return None

        if settings.require_auth and not g.user_id:
            return jsonify(error="unauthorized", detail="missing gateway identity"), 401
        if settings.require_auth and settings.admin_only and not g.is_admin:
            return jsonify(error="forbidden", detail="admin only"), 403
        return None


def _register_api(app: Flask, settings: Settings) -> None:
    from .api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix="/api")


def _register_static(app: Flask, settings: Settings) -> None:
    webroot = settings.webroot

    @app.route("/", defaults={"reqpath": ""})
    @app.route("/<path:reqpath>")
    def _static(reqpath: str):  # noqa: ANN202
        full = os.path.normpath(os.path.join(webroot, reqpath))
        if not full.startswith(os.path.abspath(webroot)):
            return "forbidden", 403
        if reqpath and os.path.isfile(full):
            rel = os.path.relpath(full, webroot)
            return send_from_directory(webroot, rel)
        index = os.path.join(webroot, "index.html")
        if os.path.isfile(index):
            return send_from_directory(webroot, "index.html")
        return (
            "<h1>RayShark</h1><p>前端未构建。webroot=%s</p>" % webroot,
            200,
            {"Content-Type": "text/html; charset=utf-8"},
        )
