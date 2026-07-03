"""REST API 蓝图。

路由分组：
- /ping /whoami /system      基础（P0）
- /nodes ...                 VMess 节点 CRUD + 导入 vmess:// （P2）
- /proxy/...                 代理启停/状态/测速/切换节点（P2）
- /capture/...               抓包启停/状态/CA 安装/iptables（P3）
- /flows ...                 流量列表/清空（P4，实时增量走 WebSocket）
- /_ingest/flow              内部：mitm addon 回传流量（仅本机回环）
"""
import logging
import platform
import time

from flask import Blueprint, current_app, g, jsonify, request

from . import __version__
from .db import get_db
from .proxy import get_proxy, parse_vmess_link
from .capture import get_capture
from .procman import get_procman
from . import ws as ws_mod

log = logging.getLogger("rayshark.api")

bp = Blueprint("api", __name__)

_STARTED_AT = time.time()


def _settings():
    return current_app.config["RAYSHARK"]


# ----------------------------- 基础 -----------------------------
@bp.get("/ping")
def ping():
    return jsonify(ok=True, app="rayshark", version=__version__,
                   uptime=round(time.time() - _STARTED_AT, 1))


@bp.get("/whoami")
def whoami():
    return jsonify(user_id=getattr(g, "user_id", ""),
                   username=getattr(g, "username", ""),
                   is_admin=getattr(g, "is_admin", False))


@bp.get("/system")
def system():
    return jsonify(machine=platform.machine(), system=platform.system(),
                   release=platform.release(), python=platform.python_version())


@bp.get("/overview")
def overview():
    """聚合状态：给首页仪表盘用。"""
    db = get_db()
    proxy_st = get_proxy().status()
    cap_st = get_capture().status()
    return jsonify(
        version=__version__,
        active_node_id=db.get_setting("active_node_id"),
        node_count=len(db.list_nodes()),
        proxy=proxy_st,
        capture=cap_st,
        flow_count=ws_mod.get_flows().count(),
        ws_clients=ws_mod.get_hub().client_count(),
    )


# ----------------------------- 节点 CRUD -----------------------------
@bp.get("/nodes")
def list_nodes():
    db = get_db()
    active = db.get_setting("active_node_id")
    return jsonify(nodes=db.list_nodes(), active_node_id=active)


@bp.post("/nodes")
def create_node():
    data = request.get_json(silent=True) or {}
    if not data.get("address") or not data.get("uuid"):
        return jsonify(error="address 与 uuid 必填"), 400
    node_id = get_db().insert_node(data)
    return jsonify(ok=True, id=node_id), 201


@bp.post("/nodes/import")
def import_node():
    """导入 vmess:// 分享链接（支持多行批量）。"""
    data = request.get_json(silent=True) or {}
    text = data.get("link") or data.get("links") or ""
    links = [x.strip() for x in text.splitlines() if x.strip()]
    if not links:
        return jsonify(error="未提供 vmess:// 链接"), 400
    db = get_db()
    created, errors = [], []
    for link in links:
        try:
            node = parse_vmess_link(link)
            created.append(db.insert_node(node))
        except ValueError as e:
            errors.append({"link": link[:40], "error": str(e)})
    return jsonify(ok=True, created=created, errors=errors), 201


@bp.put("/nodes/<int:node_id>")
def update_node(node_id: int):
    data = request.get_json(silent=True) or {}
    ok = get_db().update_node(node_id, data)
    if not ok:
        return jsonify(error="节点不存在或无可更新字段"), 404
    return jsonify(ok=True)


@bp.delete("/nodes/<int:node_id>")
def delete_node(node_id: int):
    db = get_db()
    ok = db.delete_node(node_id)
    if db.get_setting("active_node_id") == node_id:
        db.set_setting("active_node_id", None)
    return (jsonify(ok=True) if ok else (jsonify(error="节点不存在"), 404))


@bp.post("/nodes/<int:node_id>/test")
def test_node(node_id: int):
    node = get_db().get_node(node_id)
    if not node:
        return jsonify(error="节点不存在"), 404
    return jsonify(get_proxy().test_node(node))


# ----------------------------- 代理控制 -----------------------------
@bp.get("/proxy/status")
def proxy_status():
    return jsonify(get_proxy().status())


@bp.post("/proxy/start")
def proxy_start():
    """启动/切换到指定节点（body: {node_id}）。"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    node_id = data.get("node_id") or db.get_setting("active_node_id")
    if not node_id:
        return jsonify(error="未指定节点"), 400
    node = db.get_node(int(node_id))
    if not node:
        return jsonify(error="节点不存在"), 404
    res = get_proxy().reload(node)
    if res.get("ok"):
        db.set_setting("active_node_id", int(node_id))
        ws_mod.publish_event("proxy_started", {"node_id": int(node_id), "name": node["name"]})
    return jsonify(res)


@bp.post("/proxy/stop")
def proxy_stop():
    res = get_proxy().stop()
    ws_mod.publish_event("proxy_stopped", {})
    return jsonify(res)


# ----------------------------- 抓包控制 -----------------------------
@bp.get("/capture/status")
def capture_status():
    return jsonify(get_capture().status())


@bp.post("/capture/start")
def capture_start():
    data = request.get_json(silent=True) or {}
    ports = data.get("ports")
    cap = get_capture()
    if not cap.ca_installed_in_system():
        return jsonify(error="系统 CA 未安装，无法解密 HTTPS。请先调用 /capture/ca/install",
                       need_ca=True), 409
    res = cap.start_capture(ports)
    if res.get("ok"):
        sid = get_db().start_session(scope=f"ports={ports or cap.ports}")
        get_db().set_setting("current_session", sid)
        ws_mod.publish_event("capture_started", {"session_id": sid, "ports": cap.ports})
    return jsonify(res)


@bp.post("/capture/stop")
def capture_stop():
    cap = get_capture()
    res = cap.stop_capture()
    sid = get_db().get_setting("current_session")
    if sid:
        get_db().stop_session(int(sid), ws_mod.get_flows().count())
        get_db().set_setting("current_session", None)
    ws_mod.publish_event("capture_stopped", {})
    return jsonify(res)


@bp.post("/capture/ca/install")
def capture_ca_install():
    return jsonify(get_capture().install_system_ca())


@bp.post("/capture/ca/uninstall")
def capture_ca_uninstall():
    return jsonify(get_capture().uninstall_system_ca())


@bp.get("/capture/ca/cert")
def capture_ca_cert():
    """下载 CA 证书（供用户手动装到其它设备）。"""
    cap = get_capture()
    if not cap.ca_exists():
        cap.ensure_ca()
    if not cap.ca_exists():
        return jsonify(error="CA 未生成"), 404
    with open(cap.ca_cert_path(), "rb") as f:
        data = f.read()
    return (data, 200, {
        "Content-Type": "application/x-pem-file",
        "Content-Disposition": "attachment; filename=rayshark-ca.pem",
    })


# ----------------------------- 流量 -----------------------------
@bp.get("/flows")
def list_flows():
    since = int(request.args.get("since", "0"))
    return jsonify(flows=ws_mod.get_flows().snapshot(since_seq=since),
                   count=ws_mod.get_flows().count())


@bp.post("/flows/clear")
def clear_flows():
    ws_mod.get_flows().clear()
    return jsonify(ok=True)


# ----------------------------- 进程日志 -----------------------------
@bp.get("/logs/<name>")
def proc_log(name: str):
    if name not in ("v2ray", "mitmdump"):
        return jsonify(error="unknown proc"), 404
    n = int(request.args.get("n", "100"))
    return jsonify(name=name, log=get_procman().tail(name, n))


# ----------------------------- 内部：流量回传 -----------------------------
@bp.post("/_ingest/flow")
def ingest_flow():
    """mitm addon 回传流量。仅接受本机回环请求。"""
    remote = request.remote_addr or ""
    if remote not in ("127.0.0.1", "::1", "localhost", ""):
        return jsonify(error="forbidden"), 403
    flow = request.get_json(silent=True) or {}
    ws_mod.publish_flow(flow)
    return jsonify(ok=True)
