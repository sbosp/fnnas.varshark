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

log = logging.getLogger("rayshark.capture")

PROC_NAME = "mitmdump"
CHAIN = "RAYSHARK_OUT"
DEFAULT_MITM_PORT = 8080
DEFAULT_PORTS = [80, 443]
# 运行 mitm 的专用用户（安装脚本创建），其出站流量不被重定向，避免回环
MITM_USER = os.environ.get("RAYSHARK_MITM_USER", "rayshark")


def _run(argv: List[str], check: bool = False, timeout: int = 15) -> subprocess.CompletedProcess:
    log.info("exec: %s", " ".join(argv))
    return subprocess.run(argv, capture_output=True, text=True,
                          check=check, timeout=timeout)


class CaptureManager:
    def __init__(self, var_dir: str, mitm_bin: str, addon_path: str,
                 ingest_port: int, upstream_proxy: Optional[str] = None):
        self.var_dir = var_dir
        self.mitm_bin = mitm_bin
        self.addon_path = addon_path
        self.ingest_port = ingest_port
        self.upstream_proxy = upstream_proxy  # 形如 http://127.0.0.1:11081，抓完走 v2ray 出海
        self.mitm_port = DEFAULT_MITM_PORT
        self.ports = list(DEFAULT_PORTS)
        self.confdir = os.path.join(var_dir, "mitmproxy")
        os.makedirs(self.confdir, exist_ok=True)
        self._rules_applied = False

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

    # ---------------- iptables 重定向 ----------------
    def _iptables_available(self) -> bool:
        return shutil.which("iptables") is not None

    def apply_iptables(self, ports: Optional[List[int]] = None) -> Dict[str, Any]:
        """创建 RAYSHARK_OUT 链并把本机出站指定端口 REDIRECT 到 mitm 端口。

        只影响 nat/OUTPUT，且全部规则挂自定义链，卸载时整链删除可干净回滚。
        用 --uid-owner 排除 mitm 运行用户，避免 mitm 上游请求被再次重定向（回环）。
        """
        if not self._iptables_available():
            return {"ok": False, "error": "iptables 不可用"}
        ports = ports or self.ports
        self.ports = ports

        # route_localnet：允许把包重定向到 127.0.0.1
        try:
            _run(["sysctl", "-w", "net.ipv4.conf.all.route_localnet=1"])
        except Exception as e:  # noqa: BLE001
            log.warning("set route_localnet failed: %s", e)

        # 先清理残留
        self.clear_iptables(silent=True)

        try:
            _run(["iptables", "-t", "nat", "-N", CHAIN])
            # 排除发往局域网/本机自身的流量（只抓公网出站）
            for net in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
                        "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/4"):
                _run(["iptables", "-t", "nat", "-A", CHAIN, "-d", net, "-j", "RETURN"])
            # 排除 mitm 运行用户自己的出站，避免回环
            _run(["iptables", "-t", "nat", "-A", CHAIN,
                  "-m", "owner", "--uid-owner", MITM_USER, "-j", "RETURN"])
            # 命中端口 REDIRECT 到 mitm
            for port in ports:
                _run(["iptables", "-t", "nat", "-A", CHAIN,
                      "-p", "tcp", "--dport", str(port),
                      "-j", "REDIRECT", "--to-ports", str(self.mitm_port)])
            # 从 OUTPUT 跳转到自定义链
            _run(["iptables", "-t", "nat", "-A", "OUTPUT", "-p", "tcp", "-j", CHAIN])
            self._rules_applied = True
            return {"ok": True, "ports": ports, "redirect_to": self.mitm_port}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def clear_iptables(self, silent: bool = False) -> Dict[str, Any]:
        """删除跳转 + flush + 删链，干净回滚。"""
        if not self._iptables_available():
            return {"ok": False, "error": "iptables 不可用"}
        # 删 OUTPUT 里的跳转（可能有多条，循环删干净）
        for _ in range(5):
            r = _run(["iptables", "-t", "nat", "-D", "OUTPUT", "-p", "tcp", "-j", CHAIN])
            if r.returncode != 0:
                break
        _run(["iptables", "-t", "nat", "-F", CHAIN])
        _run(["iptables", "-t", "nat", "-X", CHAIN])
        self._rules_applied = False
        if not silent:
            log.info("iptables rules cleared")
        return {"ok": True}

    def iptables_active(self) -> bool:
        if not self._iptables_available():
            return False
        r = _run(["iptables", "-t", "nat", "-C", "OUTPUT", "-p", "tcp", "-j", CHAIN])
        return r.returncode == 0

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
        if self.upstream_proxy:
            # 抓完的流量走 v2ray 出海：上游模式
            argv += ["--mode", f"upstream:{self.upstream_proxy}"]
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
    def start_capture(self, ports: Optional[List[int]] = None) -> Dict[str, Any]:
        """启动完整抓包：mitm 进程 + iptables 重定向。"""
        r1 = self.start_mitm()
        if not r1.get("ok"):
            return {"ok": False, "stage": "mitm", "detail": r1}
        r2 = self.apply_iptables(ports)
        if not r2.get("ok"):
            self.stop_mitm()
            return {"ok": False, "stage": "iptables", "detail": r2}
        return {"ok": True, "mitm": r1, "iptables": r2}

    def stop_capture(self) -> Dict[str, Any]:
        r2 = self.clear_iptables()
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
            "upstream_proxy": self.upstream_proxy,
        }


_INSTANCE: Optional[CaptureManager] = None


def init_capture(var_dir: str, mitm_bin: str, addon_path: str,
                 ingest_port: int, upstream_proxy: Optional[str] = None) -> CaptureManager:
    global _INSTANCE
    _INSTANCE = CaptureManager(var_dir, mitm_bin, addon_path, ingest_port, upstream_proxy)
    return _INSTANCE


def get_capture() -> CaptureManager:
    if _INSTANCE is None:
        raise RuntimeError("capture not initialized")
    return _INSTANCE
