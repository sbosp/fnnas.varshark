"""抓包引擎控制器（需 root）。

三件事：
1. mitmdump 透明代理进程   —— 起 mitmdump --mode transparent，加载 mitm_addon
2. iptables 流量重定向     —— 把 NAS 本机出站 80/443 REDIRECT 到 mitmproxy 端口
                              (注意：抓的是本机 OUTPUT，不是转发 FORWARD)
3. mitmproxy CA 注入       —— 把 mitmproxy 生成的 CA 装进系统信任库，才能解 HTTPS

关键设计：
- iptables 规则全部挂在自定义链 RAYSHARK_OUT 上，安装=创建链+跳转，
  卸载=删跳转+flush+删链，绝不污染系统已有规则，可干净回滚。
- 为避免抓包流量自我循环，mitmproxy 进程以指定 uid 运行或标记，
  该 uid 的出站流量用 `-m owner --uid-owner` 排除（否则 mitm 自己的上游请求又被重定向 → 死循环）。
  这里采用更稳的方案：mitmproxy 以 root 跑，用 --uid-owner root 排除 root 发起的流量？
  不行——本机大量服务也是 root。改用专用标记：给 mitmdump 所在进程组打 owner uid，
  实际落地用一个专用系统用户 rayshark 运行 mitm，OUTPUT 排除该 uid。
  为降低部署复杂度，P3 先用 --uid-owner 排除 mitm 运行用户（安装时创建 rayshark 用户）。

透明模式要求内核开启 ip_forward 与 route_localnet（本机重定向到 localhost:port）。
"""
import logging
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from .procman import get_procman
from .proxy import TRANSPARENT_PORT, SO_MARK

log = logging.getLogger("rayshark.capture")

PROC_NAME = "mitmdump"
CHAIN = "RAYSHARK_OUT"
DEFAULT_MITM_PORT = 8080
DEFAULT_PORTS = [80, 443]
# 默认忽略(透传不解密)的 host 正则：这些客户端多带自有 CA / 证书固定(pinning)，
# 被 mitm 重签证书会 TLS 失败导致"加载失败/网络不通"。透传后直连原始目标、
# 既不断网也不出现在抓包列表。用户可在此基础上增删。
# 说明：--ignore-hosts 是对 "host:port" 做 re.search，故用宽松子串式正则即可。
DEFAULT_IGNORE_HOSTS = [
    r"fnnas\.com",       # 飞牛官方主域(应用中心/账号/更新)
    r"fnnas\.cn",
    r"fnos\.com",
    r"fnos\.cn",
    r"trim\.cn",         # 飞牛旧域/CDN
    r"apple\.com",       # 系统类证书固定服务，避免误伤
    r"icloud\.com",
    r"googleapis\.com",
]
# 私网/回环/链路本地：这些目的地一律直连，不进代理也不抓包
PRIVATE_NETS = (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
    "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/4",
)
# 运行 mitm 的专用用户（安装脚本创建），其出站流量不被重定向到 mitm，避免回环
MITM_USER = os.environ.get("RAYSHARK_MITM_USER", "rayshark")


def _run(argv: List[str], check: bool = False, timeout: int = 15) -> subprocess.CompletedProcess:
    log.info("exec: %s", " ".join(argv))
    return subprocess.run(argv, capture_output=True, text=True,
                          check=check, timeout=timeout)


class CaptureManager:
    def __init__(self, var_dir: str, mitm_bin: str, addon_path: str,
                 ingest_port: int, transparent_port: int = TRANSPARENT_PORT):
        self.var_dir = var_dir
        self.mitm_bin = mitm_bin
        self.addon_path = addon_path
        self.ingest_port = ingest_port
        self.transparent_port = transparent_port  # v2ray dokodemo 透明入站口
        self.mitm_port = DEFAULT_MITM_PORT
        self.ports = list(DEFAULT_PORTS)
        # 透传(不解密)的 host 正则名单，防止对证书固定客户端(如飞牛应用中心)断网
        self.ignore_hosts = list(DEFAULT_IGNORE_HOSTS)
        self.confdir = os.path.join(var_dir, "mitmproxy")
        os.makedirs(self.confdir, exist_ok=True)
        # 两个独立开关，共同决定 iptables 链形态：
        #   global_on  = v2ray 全局透明代理是否接管本机出站
        #   capture_on = 是否在其上叠加 mitmproxy 解密抓包(80/443)
        self._global_on = False
        self._capture_on = False

    # ---------------- 二进制/证书探测 ----------------
    def binary_exists(self) -> bool:
        return bool(self.mitm_bin) and (
            os.path.isfile(self.mitm_bin) or shutil.which(self.mitm_bin) is not None
        )

    def ca_cert_path(self) -> str:
        # mitmproxy 首次运行会在 confdir 生成 mitmproxy-ca-cert.pem
        return os.path.join(self.confdir, "mitmproxy-ca-cert.pem")

    def ca_exists(self) -> bool:
        return os.path.isfile(self.ca_cert_path())

    def ensure_ca(self) -> Dict[str, Any]:
        """确保 CA 已生成：跑一次 mitmdump 快速退出以生成证书。"""
        if self.ca_exists():
            return {"ok": True, "generated": False, "path": self.ca_cert_path()}
        if not self.binary_exists():
            return {"ok": False, "error": "mitmdump 未安装"}
        # 起一个瞬时进程，生成证书后杀掉
        try:
            p = subprocess.Popen(
                [self._mitm(), "--set", f"confdir={self.confdir}",
                 "-p", "0", "-n"],  # -n 不用默认脚本, -p 0 随机端口
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(25):
                if self.ca_exists():
                    break
                time.sleep(0.2)
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"生成 CA 失败: {e}"}
        return {"ok": self.ca_exists(), "generated": True, "path": self.ca_cert_path()}

    def _mitm(self) -> str:
        if os.path.isfile(self.mitm_bin):
            return self.mitm_bin
        return shutil.which(self.mitm_bin) or self.mitm_bin

    # ---------------- 系统 CA 信任库 ----------------
    def install_system_ca(self) -> Dict[str, Any]:
        """把 mitmproxy CA 注入 Debian 系统信任库（飞牛基于 Debian）。"""
        if not self.ca_exists():
            r = self.ensure_ca()
            if not r.get("ok"):
                return r
        src = self.ca_cert_path()
        dst_dir = "/usr/local/share/ca-certificates"
        dst = os.path.join(dst_dir, "rayshark-mitmproxy.crt")
        try:
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(src, dst)  # .crt 扩展名是 update-ca-certificates 要求
            r = _run(["update-ca-certificates"])
            ok = r.returncode == 0
            return {
                "ok": ok, "installed_path": dst,
                "output": (r.stdout + r.stderr)[-500:],
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def uninstall_system_ca(self) -> Dict[str, Any]:
        dst = "/usr/local/share/ca-certificates/rayshark-mitmproxy.crt"
        try:
            if os.path.isfile(dst):
                os.unlink(dst)
            r = _run(["update-ca-certificates", "--fresh"])
            return {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-500:]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def ca_installed_in_system(self) -> bool:
        return os.path.isfile("/usr/local/share/ca-certificates/rayshark-mitmproxy.crt")

    # ---------------- iptables（全局代理 + 可选抓包 统一管理） ----------------
    def _iptables_available(self) -> bool:
        return shutil.which("iptables") is not None

    def _rebuild_chain(self) -> Dict[str, Any]:
        """按当前 (global_on, capture_on) 两个标志，原子重建整条 RAYSHARK_OUT 链。

        规则顺序（命中即止，靠 RETURN/REDIRECT 短路）：
          1. 带 SO_MARK(v2ray 自身出海流量) -> RETURN     防代理流量回环
          2. mitm 运行用户的出站          -> RETURN       防抓包解密后上游请求回环
          3. 私网/回环/组播目的地          -> RETURN       内网与本机直连
          4. [抓包开] tcp 80/443          -> REDIRECT mitm 端口   叠加解密
          5. [全局开] 其余 tcp/udp         -> REDIRECT v2ray 透明口  全局接管

        全部规则挂自定义链，OUTPUT 仅一条 -j 跳转；关闭时整链删除干净回滚。
        capture_on 为真时必须 global_on 也为真（抓包是全局代理之上的叠加）。
        """
        if not self._iptables_available():
            return {"ok": False, "error": "iptables 不可用"}

        # route_localnet：允许把包重定向到 127.0.0.1 上的本机端口
        try:
            _run(["sysctl", "-w", "net.ipv4.conf.all.route_localnet=1"])
        except Exception as e:  # noqa: BLE001
            log.warning("set route_localnet failed: %s", e)

        # 先彻底清理，保证幂等
        self._flush_chain(silent=True)

        # 两个开关都关：清空即可，不建链
        if not self._global_on and not self._capture_on:
            return {"ok": True, "global": False, "capture": False}

        try:
            _run(["iptables", "-t", "nat", "-N", CHAIN])
            # 1) 放行 v2ray 自身出海（按 fwmark），这是防回环的第一道闸
            _run(["iptables", "-t", "nat", "-A", CHAIN,
                  "-m", "mark", "--mark", str(SO_MARK), "-j", "RETURN"])
            # 2) 放行 mitm 运行用户的出站（解密后走 v2ray 时不再被 mitm 截获）
            _run(["iptables", "-t", "nat", "-A", CHAIN,
                  "-m", "owner", "--uid-owner", MITM_USER, "-j", "RETURN"])
            # 3) 私网/回环/组播直连
            for net in PRIVATE_NETS:
                _run(["iptables", "-t", "nat", "-A", CHAIN, "-d", net, "-j", "RETURN"])
            # 3.5) DNS(53) 一律直连，绝不导入透明口。
            #   关键防环：v2ray 解析节点域名发出的 DNS 若被抓回透明口，会与自身
            #   无限打转，直接吃满 CPU。DNS 交给系统 resolver 直连即可(节点通常用
            #   IP；即便用域名，此处直连解析后 v2ray 再按 IP 出海，不会回环)。
            _run(["iptables", "-t", "nat", "-A", CHAIN,
                  "-p", "udp", "--dport", "53", "-j", "RETURN"])
            _run(["iptables", "-t", "nat", "-A", CHAIN,
                  "-p", "tcp", "--dport", "53", "-j", "RETURN"])
            # 4) 抓包叠加：80/443 先转 mitm 解密（mitm 上游请求已在第 2 步放行）
            if self._capture_on:
                for port in self.ports:
                    _run(["iptables", "-t", "nat", "-A", CHAIN,
                          "-p", "tcp", "--dport", str(port),
                          "-j", "REDIRECT", "--to-ports", str(self.mitm_port)])
            # 5) 全局接管：TCP 全部导入 v2ray 透明入站。
            #   UDP 只导 443(QUIC/HTTP3)——其余 UDP(NTP/mDNS/游戏等)直连，避免
            #   把大量无需代理的 UDP 卷进 dokodemo-door 造成空转与高负载。
            if self._global_on:
                _run(["iptables", "-t", "nat", "-A", CHAIN,
                      "-p", "tcp",
                      "-j", "REDIRECT", "--to-ports", str(self.transparent_port)])
                _run(["iptables", "-t", "nat", "-A", CHAIN,
                      "-p", "udp", "--dport", "443",
                      "-j", "REDIRECT", "--to-ports", str(self.transparent_port)])
            # OUTPUT 挂唯一跳转
            _run(["iptables", "-t", "nat", "-A", "OUTPUT", "-j", CHAIN])
            return {
                "ok": True,
                "global": self._global_on,
                "capture": self._capture_on,
                "capture_ports": self.ports if self._capture_on else [],
                "transparent_port": self.transparent_port,
                "mitm_port": self.mitm_port,
            }
        except Exception as e:  # noqa: BLE001
            # 出错回滚，避免留下半条链
            self._flush_chain(silent=True)
            return {"ok": False, "error": str(e)}

    def _flush_chain(self, silent: bool = False) -> Dict[str, Any]:
        """删 OUTPUT 跳转 + flush + 删链。兼容旧版 `-p tcp -j CHAIN` 跳转。"""
        if not self._iptables_available():
            return {"ok": False, "error": "iptables 不可用"}
        for jump in (["-j", CHAIN], ["-p", "tcp", "-j", CHAIN]):
            for _ in range(5):
                r = _run(["iptables", "-t", "nat", "-D", "OUTPUT", *jump])
                if r.returncode != 0:
                    break
        _run(["iptables", "-t", "nat", "-F", CHAIN])
        _run(["iptables", "-t", "nat", "-X", CHAIN])
        if not silent:
            log.info("iptables chain %s cleared", CHAIN)
        return {"ok": True}

    # ---- 全局代理开关（由 /proxy/start|stop 联动）----
    def enable_global(self) -> Dict[str, Any]:
        self._global_on = True
        return self._rebuild_chain()

    def disable_global(self) -> Dict[str, Any]:
        """关闭全局代理。同时会关掉抓包（抓包依赖全局链存在）。"""
        self._global_on = False
        self._capture_on = False
        return self._rebuild_chain()

    def global_active(self) -> bool:
        return self.chain_installed() and self._global_on

    # ---- 兼容旧接口：整链是否挂在 OUTPUT 上 ----
    def chain_installed(self) -> bool:
        if not self._iptables_available():
            return False
        r = _run(["iptables", "-t", "nat", "-C", "OUTPUT", "-j", CHAIN])
        return r.returncode == 0

    def iptables_active(self) -> bool:
        # 保留旧名：链已安装即视为有重定向生效
        return self.chain_installed()

    # ---------------- mitmdump 进程 ----------------
    def _chown_confdir(self, username: str) -> None:
        """把 confdir 交给 mitm 运行用户，使其降权后仍能读写 CA/证书缓存。"""
        import pwd
        try:
            pw = pwd.getpwnam(username)
        except KeyError:
            return
        for root, dirs, files in os.walk(self.confdir):
            try:
                os.chown(root, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass
            for n in files:
                try:
                    os.chown(os.path.join(root, n), pw.pw_uid, pw.pw_gid)
                except OSError:
                    pass

    def start_mitm(self) -> Dict[str, Any]:
        if not self.binary_exists():
            return {"ok": False, "error": "mitmdump 未安装"}
        # 确保 CA 就绪（以当前用户/ root 生成，随后移交 mitm 用户）
        self.ensure_ca()
        # 若目标运行用户存在，把 confdir 移交它；否则不降权（回退 root）
        run_as = MITM_USER if self._user_exists(MITM_USER) else None
        if run_as:
            self._chown_confdir(run_as)
        env = {
            "RAYSHARK_INGEST_URL": f"http://127.0.0.1:{self.ingest_port}/api/_ingest/flow",
        }
        argv = [
            self._mitm(),
            "--mode", "transparent",
            "--showhost",
            "--set", f"confdir={self.confdir}",
            "--listen-host", "0.0.0.0",
            "-p", str(self.mitm_port),
            "-s", self.addon_path,
            "-q",  # 安静，日志由 addon/后端管
        ]
        # 忽略名单：匹配的 host 透传不解密(TCP 直连转发)，避免证书固定客户端
        # (飞牛应用中心等)因 mitm 重签证书而 TLS 失败断网。mitmproxy 接受多个
        # --ignore-hosts，各自是对 "host:port" 的正则。
        for pat in self.ignore_hosts:
            if pat and pat.strip():
                argv += ["--ignore-hosts", pat.strip()]
        # 注意：不再用 --mode upstream。mitm 解密后的上游请求以 rayshark 用户
        # 发出，会重新经过 iptables 链，被第 5 条规则导入 v2ray 透明入站出海，
        # 从而天然实现“抓包 + 全局代理”叠加，无需 mitm 自己接上游。
        return get_procman().start(PROC_NAME, argv, env=env, run_as=run_as)

    @staticmethod
    def _user_exists(username: str) -> bool:
        import pwd
        try:
            pwd.getpwnam(username)
            return True
        except KeyError:
            return False

    def stop_mitm(self) -> Dict[str, Any]:
        return get_procman().stop(PROC_NAME)

    # ---------------- 会话编排 ----------------
    def start_capture(self, ports: Optional[List[int]] = None,
                      ignore_hosts: Optional[List[str]] = None) -> Dict[str, Any]:
        """在全局代理之上叠加抓包：起 mitm 进程 + 把 80/443 REDIRECT 到 mitm。

        前提是全局代理已开（_global_on）。若未开则拒绝，避免只抓不出海的
        半截链路让用户困惑。

        ignore_hosts: 透传(不解密)的 host 正则名单。默认含飞牛官方域名，避免
            证书固定客户端(应用中心)被 mitm 断网。传入则覆盖默认名单。
        """
        if not self._global_on:
            return {"ok": False, "error": "请先启动代理（全局代理未开启，无法叠加抓包）",
                    "need_global": True}
        if ports:
            self.ports = ports
        if ignore_hosts is not None:
            # 覆盖：始终并入内置飞牛域名，避免用户误删导致应用中心又断网
            merged = list(DEFAULT_IGNORE_HOSTS)
            for h in ignore_hosts:
                if h and h.strip() and h.strip() not in merged:
                    merged.append(h.strip())
            self.ignore_hosts = merged
        r1 = self.start_mitm()
        if not r1.get("ok"):
            return {"ok": False, "stage": "mitm", "detail": r1}
        self._capture_on = True
        r2 = self._rebuild_chain()
        if not r2.get("ok"):
            self._capture_on = False
            self.stop_mitm()
            self._rebuild_chain()
            return {"ok": False, "stage": "iptables", "detail": r2}
        return {"ok": True, "mitm": r1, "iptables": r2}

    def stop_capture(self) -> Dict[str, Any]:
        """停止抓包，但保留全局代理。只翻转 capture 标志并重建链。"""
        self._capture_on = False
        r2 = self._rebuild_chain()
        r1 = self.stop_mitm()
        return {"ok": True, "mitm": r1, "iptables": r2}

    def status(self) -> Dict[str, Any]:
        st = get_procman().status(PROC_NAME)
        return {
            **st,
            "mitm_port": self.mitm_port,
            "ports": self.ports,
            "binary": self.binary_exists(),
            "ca_generated": self.ca_exists(),
            "ca_in_system": self.ca_installed_in_system(),
            "iptables_active": self.iptables_active(),
            "global_active": self.global_active(),
            "capture_on": self._capture_on,
            "transparent_port": self.transparent_port,
            "ignore_hosts": self.ignore_hosts,
        }


_INSTANCE: Optional[CaptureManager] = None


def init_capture(var_dir: str, mitm_bin: str, addon_path: str,
                 ingest_port: int, transparent_port: int = TRANSPARENT_PORT) -> CaptureManager:
    global _INSTANCE
    _INSTANCE = CaptureManager(var_dir, mitm_bin, addon_path, ingest_port, transparent_port)
    return _INSTANCE


def get_capture() -> CaptureManager:
    if _INSTANCE is None:
        raise RuntimeError("capture not initialized")
    return _INSTANCE
