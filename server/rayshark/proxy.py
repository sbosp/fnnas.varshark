"""V2Ray 代理管理。

职责：
- parse_vmess_link(): 解析 vmess:// 分享链接（base64 JSON 格式）为节点 dict
- build_config():     由激活节点生成 v2ray config.json
                      入站：SOCKS(本地) + HTTP(本地) —— 供 mitmproxy 上游/系统代理用
                      出站：VMess -> 远端节点
- reload():           写 config.json 并重启 v2ray 进程（热切换节点）
- test_node():        经本地 SOCKS 出口 curl 一个探测 URL，测连通与延迟

v2ray-core 二进制放 app/server/bin/v2ray（aarch64），config 写 var/v2ray/config.json。
本地入站端口：SOCKS=EXPORT_SOCKS_PORT, HTTP=EXPORT_HTTP_PORT（见常量）。
"""
import base64
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, Optional

from .procman import get_procman

log = logging.getLogger("rayshark.proxy")

PROC_NAME = "v2ray"

# 本地入站端口（仅监听 127.0.0.1）
SOCKS_PORT = 11080
HTTP_PORT = 11081
# 透明代理入站（dokodemo-door），供 iptables REDIRECT 全局流量导入。
# 监听 0.0.0.0 是因为 REDIRECT 会把目标改写为本机，但源仍是本机各进程。
TRANSPARENT_PORT = 12345
# v2ray 自身出海流量（VMess/direct 出站）打的防火墙标记；
# iptables 链里对带此标记的包直接 RETURN，避免代理流量再被重定向成环。
SO_MARK = 255


def parse_vmess_link(link: str) -> Dict[str, Any]:
    """解析 vmess:// 链接。

    标准格式：vmess://<base64(JSON)>，JSON 字段（v2rayN 约定）：
      v(版本) ps(备注) add(地址) port(端口) id(uuid) aid(alterId)
      scy(加密) net(传输) type host path tls sni
    """
    link = link.strip()
    if not link.lower().startswith("vmess://"):
        raise ValueError("不是 vmess:// 链接")
    b64 = link[len("vmess://"):].strip()
    # 补齐 base64 padding
    pad = len(b64) % 4
    if pad:
        b64 += "=" * (4 - pad)
    try:
        raw = base64.urlsafe_b64decode(b64).decode("utf-8", errors="replace")
        # 有些实现用标准 b64（+/），兜底再试
        conf = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        try:
            raw = base64.b64decode(b64).decode("utf-8", errors="replace")
            conf = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"vmess 链接解析失败: {e}")

    def _int(v, d=0):
        try:
            return int(v)
        except (ValueError, TypeError):
            return d

    net = str(conf.get("net", "tcp") or "tcp")
    tls_on = str(conf.get("tls", "")).lower() in ("tls", "1", "true")
    return {
        "name": conf.get("ps") or conf.get("add") or "vmess-node",
        "protocol": "vmess",
        "address": conf.get("add", ""),
        "port": _int(conf.get("port")),
        "uuid": conf.get("id", ""),
        "alter_id": _int(conf.get("aid")),
        "security": conf.get("scy") or "auto",
        "network": net,
        "ws_path": conf.get("path", "") if net == "ws" else "",
        "ws_host": conf.get("host", "") if net == "ws" else "",
        "tls": 1 if tls_on else 0,
        "sni": conf.get("sni", "") or conf.get("host", ""),
        "remark": conf.get("ps", ""),
        "raw": link,
    }


def build_config(node: Dict[str, Any], apply_mark: bool = True) -> Dict[str, Any]:
    """由节点生成 v2ray 4/5.x 兼容的 config。

    apply_mark: 是否给出站连接打防火墙标记(SO_MARK)。仅在需要 iptables 全局
        透明代理防回环时才需要，且设置该标记需要 CAP_NET_ADMIN(root)。测速等
        纯本地 SOCKS 探测不经 iptables，不应打标记——否则无 root 权限时
        setsockopt 会 EPERM 导致拨号失败、节点误判为"连不上"。
    """
    stream: Dict[str, Any] = {"network": node.get("network", "tcp")}
    if node.get("network") == "ws":
        ws_opts: Dict[str, Any] = {"path": node.get("ws_path") or "/"}
        if node.get("ws_host"):
            ws_opts["headers"] = {"Host": node["ws_host"]}
        stream["wsSettings"] = ws_opts
    if node.get("tls"):
        stream["security"] = "tls"
        sni = node.get("sni") or node.get("ws_host") or node.get("address")
        stream["tlsSettings"] = {"serverName": sni, "allowInsecure": False}
    # v2ray 发往节点的连接打防火墙标记，iptables 对该标记 RETURN，防止回环。
    if apply_mark:
        stream["sockopt"] = {"mark": SO_MARK}

    direct_stream: Dict[str, Any] = {"sockopt": {"mark": SO_MARK}} if apply_mark else {}

    return {
        "log": {"loglevel": "warning"},
        # 内建 DNS：v2ray 解析目标域名时用这里的服务器，经 direct 出站(带 mark)
        # 直连查询，不依赖系统 resolver，避免解析流量被 iptables 抓回透明口回环。
        "dns": {
            "servers": ["223.5.5.5", "119.29.29.29", "8.8.8.8", "1.1.1.1"],
            "queryStrategy": "UseIPv4",
        },
        "inbounds": [
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": SOCKS_PORT,
                "protocol": "socks",
                "settings": {"udp": True, "auth": "noauth"},
            },
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": HTTP_PORT,
                "protocol": "http",
                "settings": {},
            },
            {
                # 透明入站：iptables 把本机出站 REDIRECT 到此端口后，
                # dokodemo-door 用 followRedirect 从内核取回原始目的地址。
                # sniffing 探测 http/tls 以还原域名，供路由/日志使用。
                #
                # 关键：必须绑 127.0.0.1，绝不能绑 0.0.0.0。
                #   REDIRECT 会把包的目的地址重写成 127.0.0.1:12345，所以绑本地
                #   足以收到全部被重定向的流量。若绑 0.0.0.0，LAN 内主机可直连
                #   本口，而这类"非经 iptables 重定向"的连接读 SO_ORIGINAL_DST
                #   会拿到 12345 自身 → v2ray 回连 127.0.0.1:12345 → 触发新入站
                #   → 无限自环级联(fd 暴涨、多核烧满)。绑本地即从根上杜绝。
                "tag": "transparent-in",
                "listen": "127.0.0.1",
                "port": TRANSPARENT_PORT,
                "protocol": "dokodemo-door",
                "settings": {"network": "tcp,udp", "followRedirect": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
            },
        ],
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vmess",
                "settings": {
                    "vnext": [
                        {
                            "address": node["address"],
                            "port": int(node["port"]),
                            "users": [
                                {
                                    "id": node["uuid"],
                                    "alterId": int(node.get("alter_id", 0)),
                                    "security": node.get("security", "auto"),
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": stream,
            },
            {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {},
                "streamSettings": direct_stream,
            },
        ],
        # 路由：DNS 查询与私网/回环直连，其余走代理出站。
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    # v2ray 内建 DNS 产生的查询走 direct(带 mark)直连，防回环
                    "type": "field",
                    "protocol": ["dns"],
                    "outboundTag": "direct",
                },
                {
                    # 兜底：任何发往 53 端口的流量也直连
                    "type": "field",
                    "port": 53,
                    "outboundTag": "direct",
                },
                {
                    "type": "field",
                    "ip": [
                        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
                        "192.168.0.0/16", "169.254.0.0/16", "::1/128",
                        "fc00::/7", "fe80::/10",
                    ],
                    "outboundTag": "direct",
                },
            ],
        },
    }


class ProxyManager:
    def __init__(self, var_dir: str, bin_path: str):
        self.var_dir = var_dir
        self.bin_path = bin_path
        self.conf_dir = os.path.join(var_dir, "v2ray")
        self.conf_path = os.path.join(self.conf_dir, "config.json")
        os.makedirs(self.conf_dir, exist_ok=True)

    def binary_exists(self) -> bool:
        return os.path.isfile(self.bin_path) and os.access(self.bin_path, os.X_OK)

    def write_config(self, node: Dict[str, Any]) -> str:
        conf = build_config(node)
        with open(self.conf_path, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=2)
        return self.conf_path

    def reload(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """写新 config 并重启 v2ray（热切换节点）。"""
        if not self.binary_exists():
            return {"ok": False, "error": f"v2ray 二进制不存在: {self.bin_path}"}
        self.write_config(node)
        pm = get_procman()
        pm.stop(PROC_NAME)
        # v2ray 5.x: run -c ; 4.x: -config。用 run -c 更通用，回落 -config
        argv = [self.bin_path, "run", "-c", self.conf_path]
        res = pm.start(PROC_NAME, argv)
        if not res.get("ok"):
            # 回落老参数
            argv = [self.bin_path, "-config", self.conf_path]
            res = pm.start(PROC_NAME, argv)
        return res

    def stop(self) -> Dict[str, Any]:
        return get_procman().stop(PROC_NAME)

    def status(self) -> Dict[str, Any]:
        st = get_procman().status(PROC_NAME)
        st["socks_port"] = SOCKS_PORT
        st["http_port"] = HTTP_PORT
        st["transparent_port"] = TRANSPARENT_PORT
        st["binary"] = self.binary_exists()
        return st

    def test_node(self, node: Dict[str, Any], probe_url: str = "https://www.gstatic.com/generate_204",
                  timeout: int = 8) -> Dict[str, Any]:
        """临时起一个测试实例测连通性 + 延迟。

        为不打断当前代理，用独立端口 + 独立 config 起临时进程测速。
        """
        if not self.binary_exists():
            return {"ok": False, "error": "v2ray 二进制不存在"}

        test_socks = SOCKS_PORT + 100
        # 测速走纯本地 SOCKS 探测，不经 iptables，无需(也不应)打 SO_MARK，
        # 否则无 root 权限时 setsockopt 会 EPERM 导致拨号失败、误判"连不上"。
        conf = build_config(node, apply_mark=False)
        conf["inbounds"] = [
            {
                "tag": "socks-test",
                "listen": "127.0.0.1",
                "port": test_socks,
                "protocol": "socks",
                "settings": {"udp": False, "auth": "noauth"},
            }
        ]
        test_conf_path = os.path.join(self.conf_dir, "test.json")
        with open(test_conf_path, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False)

        pm = get_procman()
        pm.stop("v2ray-test")
        argv = [self.bin_path, "run", "-c", test_conf_path]
        res = pm.start("v2ray-test", argv)
        if not res.get("ok"):
            argv = [self.bin_path, "-config", test_conf_path]
            res = pm.start("v2ray-test", argv)
        if not res.get("ok"):
            return {"ok": False, "error": "测试实例启动失败", "detail": res}

        time.sleep(1.0)  # 等入站就绪
        try:
            import subprocess
            t0 = time.time()
            proc = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "--socks5-hostname", f"127.0.0.1:{test_socks}",
                 "--max-time", str(timeout), probe_url],
                capture_output=True, text=True, timeout=timeout + 3,
            )
            latency = int((time.time() - t0) * 1000)
            code = proc.stdout.strip()
            ok = code in ("204", "200")
            return {
                "ok": ok,
                "http_code": code,
                "latency_ms": latency,
                "error": "" if ok else (proc.stderr.strip() or f"unexpected code {code}"),
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        finally:
            pm.stop("v2ray-test")


_INSTANCE: Optional[ProxyManager] = None


def init_proxy(var_dir: str, bin_path: str) -> ProxyManager:
    global _INSTANCE
    _INSTANCE = ProxyManager(var_dir, bin_path)
    return _INSTANCE


def get_proxy() -> ProxyManager:
    if _INSTANCE is None:
        raise RuntimeError("proxy not initialized")
    return _INSTANCE
