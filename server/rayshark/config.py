"""运行时配置：全部来自环境变量（由 cmd/main 注入）。"""
import os
from dataclasses import dataclass


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _server_dir() -> str:
    return os.path.dirname(_here())  # app/server


def _default_webroot() -> str:
    # rayshark/ -> server/ -> app/ ; ui 在 app/ui
    app_dir = os.path.dirname(_server_dir())  # app
    return os.path.join(app_dir, "ui")


@dataclass
class Settings:
    webroot: str
    sock_path: str
    logfile: str
    gateway_prefix: str
    require_auth: bool
    admin_only: bool
    var_dir: str
    tcp_mode: bool
    tcp_host: str
    tcp_port: int
    # 数据 / 二进制路径
    db_path: str
    v2ray_bin: str
    mitm_bin: str
    addon_path: str
    ingest_port: int
    flow_buffer_size: int

    @classmethod
    def from_env(cls) -> "Settings":
        appdest = os.environ.get("TRIM_APPDEST", "")
        server_dir = os.path.join(appdest, "server") if appdest else _server_dir()
        webroot = os.environ.get("RAYSHARK_WEBROOT") or (
            os.path.join(appdest, "ui") if appdest else _default_webroot()
        )
        sock = os.environ.get("RAYSHARK_SOCK") or (
            os.path.join(appdest, "app.sock") if appdest else "/tmp/rayshark.sock"
        )
        var_dir = os.environ.get("TRIM_PKGVAR") or os.environ.get(
            "RAYSHARK_VAR", "/tmp/rayshark-var"
        )
        logfile = os.environ.get("RAYSHARK_LOGFILE") or os.path.join(
            var_dir, "rayshark.log"
        )
        db_path = os.environ.get("RAYSHARK_DB") or os.path.join(var_dir, "rayshark.db")
        # aarch64 二进制随包放 server/bin/
        bin_dir = os.path.join(server_dir, "bin")
        v2ray_bin = os.environ.get("RAYSHARK_V2RAY_BIN") or os.path.join(bin_dir, "v2ray")
        # mitmdump：优先随包二进制，否则回落 PATH（开发机 pip 装的）
        mitm_bin = os.environ.get("RAYSHARK_MITM_BIN") or os.path.join(bin_dir, "mitmdump")
        addon_path = os.environ.get("RAYSHARK_ADDON") or os.path.join(
            server_dir, "rayshark", "mitm_addon.py"
        )
        return cls(
            webroot=webroot,
            sock_path=sock,
            logfile=logfile,
            gateway_prefix=os.environ.get("RAYSHARK_GATEWAY_PREFIX", "/app/fnnas-rayshark"),
            require_auth=os.environ.get("RAYSHARK_REQUIRE_AUTH", "0") == "1",
            admin_only=os.environ.get("RAYSHARK_ADMIN_ONLY", "1") == "1",
            var_dir=var_dir,
            tcp_mode=os.environ.get("RAYSHARK_TCP", "0") == "1",
            tcp_host=os.environ.get("RAYSHARK_TCP_HOST", "127.0.0.1"),
            tcp_port=int(os.environ.get("RAYSHARK_TCP_PORT", "8899")),
            db_path=db_path,
            v2ray_bin=v2ray_bin,
            mitm_bin=mitm_bin,
            addon_path=addon_path,
            ingest_port=int(os.environ.get("RAYSHARK_INGEST_PORT", "11090")),
            flow_buffer_size=int(os.environ.get("RAYSHARK_FLOW_BUFFER", "1000")),
        )
