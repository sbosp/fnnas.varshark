"""子进程编排器。

统一管理长期运行的子进程（v2ray-core / mitmdump），提供：
- start(name, argv, env) : 拉起进程，记录 pid，重定向日志到文件
- stop(name)            : 优雅 TERM，超时 KILL
- status(name)          : 是否存活 + pid + 运行时长
- tail(name, n)         : 读日志尾部
- is_alive(name)

设计要点：
- 用 subprocess.Popen（gevent monkey patch 后 os.waitpid 是协程友好的）。
- 每个受管进程一份日志文件（var/<name>.log），前端可拉取。
- 进程对象存内存字典；重启后端会丢失句柄，但通过 pidfile 兜底 stop。
"""
import os
import signal
import subprocess
import time
from typing import Dict, List, Optional

import logging

log = logging.getLogger("rayshark.procman")


class _Proc:
    def __init__(self, name: str, popen: subprocess.Popen, logfile: str, argv: List[str]):
        self.name = name
        self.popen = popen
        self.logfile = logfile
        self.argv = argv
        self.started_at = time.time()


class ProcessManager:
    def __init__(self, var_dir: str):
        self.var_dir = var_dir
        self.run_dir = os.path.join(var_dir, "run")
        self.log_dir = os.path.join(var_dir, "logs")
        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        self._procs: Dict[str, _Proc] = {}

    def _pidfile(self, name: str) -> str:
        return os.path.join(self.run_dir, f"{name}.pid")

    def _logfile(self, name: str) -> str:
        return os.path.join(self.log_dir, f"{name}.log")

    def _read_pid(self, name: str) -> Optional[int]:
        try:
            with open(self._pidfile(name)) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def is_alive(self, name: str) -> bool:
        p = self._procs.get(name)
        if p and p.popen.poll() is None:
            return True
        pid = self._read_pid(name)
        return self._alive(pid) if pid else False

    @staticmethod
    def _make_drop_priv(username: str, env: Dict[str, str]):
        """返回一个 preexec_fn：子进程 fork 后、exec 前降权到指定用户。

        找不到该用户则返回 None（不降权，退回 root 运行——抓包回环风险，
        但至少不崩溃；上层会在创建用户后正常降权）。
        """
        import pwd
        try:
            pw = pwd.getpwnam(username)
        except KeyError:
            log.warning("run_as user %s not found, running without drop-priv", username)
            return None
        uid, gid, home = pw.pw_uid, pw.pw_gid, pw.pw_dir

        def _drop():
            os.setgid(gid)
            try:
                os.initgroups(username, gid)
            except (OSError, PermissionError):
                pass
            os.setuid(uid)
            os.environ["HOME"] = home
            env["HOME"] = home

        # env HOME 需在父进程侧也更新，供 mitmproxy confdir 等使用
        env.setdefault("HOME", home)
        return _drop

    def start(self, name: str, argv: List[str], env: Optional[Dict[str, str]] = None,
              cwd: Optional[str] = None, run_as: Optional[str] = None) -> Dict:
        if self.is_alive(name):
            return {"ok": True, "already": True, **self.status(name)}

        logfile = self._logfile(name)
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        # 可选：以指定系统用户运行（抓包时 mitmdump 需降权到 rayshark 用户，
        # 配合 iptables --uid-owner 排除自身出站，避免重定向回环）。
        preexec = None
        if run_as:
            preexec = self._make_drop_priv(run_as, full_env)

        log.info("starting proc %s: %s%s", name, " ".join(argv),
                 f" (as {run_as})" if run_as else "")
        with open(logfile, "ab", buffering=0) as lf:
            lf.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} : {' '.join(argv)} ===\n".encode())
            popen = subprocess.Popen(
                argv, stdout=lf, stderr=subprocess.STDOUT,
                env=full_env, cwd=cwd, start_new_session=True,
                preexec_fn=preexec,
            )
        self._procs[name] = _Proc(name, popen, logfile, argv)
        with open(self._pidfile(name), "w") as f:
            f.write(str(popen.pid))

        # 给进程一点启动时间，尽早暴露崩溃
        time.sleep(0.4)
        if popen.poll() is not None:
            log.error("proc %s exited immediately code=%s", name, popen.returncode)
            return {
                "ok": False,
                "error": "process exited immediately",
                "code": popen.returncode,
                "log_tail": self.tail(name, 30),
            }
        return {"ok": True, **self.status(name)}

    def stop(self, name: str, timeout: int = 8) -> Dict:
        p = self._procs.get(name)
        pid = p.popen.pid if p else self._read_pid(name)
        if not pid or not self._alive(pid):
            self._cleanup(name)
            return {"ok": True, "already_stopped": True}

        log.info("stopping proc %s pid=%s", name, pid)
        try:
            # 杀整个进程组（start_new_session=True 时 pgid==pid）
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        waited = 0.0
        while self._alive(pid) and waited < timeout:
            time.sleep(0.3)
            waited += 0.3
        if self._alive(pid):
            log.warning("proc %s did not exit, KILL", name)
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self._cleanup(name)
        return {"ok": True}

    def _cleanup(self, name: str) -> None:
        self._procs.pop(name, None)
        try:
            os.unlink(self._pidfile(name))
        except OSError:
            pass

    def status(self, name: str) -> Dict:
        alive = self.is_alive(name)
        p = self._procs.get(name)
        pid = p.popen.pid if p else self._read_pid(name)
        uptime = round(time.time() - p.started_at, 1) if (p and alive) else 0
        return {
            "name": name,
            "alive": alive,
            "pid": pid if alive else None,
            "uptime": uptime,
        }

    def tail(self, name: str, n: int = 100) -> str:
        logfile = self._logfile(name)
        if not os.path.isfile(logfile):
            return ""
        try:
            with open(logfile, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                block = min(size, 64 * 1024)
                f.seek(size - block)
                data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return ""

    def stop_all(self) -> None:
        for name in list(self._procs.keys()):
            try:
                self.stop(name)
            except Exception as e:  # noqa: BLE001
                log.warning("stop %s failed: %s", name, e)


_INSTANCE: Optional[ProcessManager] = None


def init_procman(var_dir: str) -> ProcessManager:
    global _INSTANCE
    _INSTANCE = ProcessManager(var_dir)
    return _INSTANCE


def get_procman() -> ProcessManager:
    if _INSTANCE is None:
        raise RuntimeError("procman not initialized")
    return _INSTANCE
