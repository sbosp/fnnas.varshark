"""SQLite 数据层。

存放：
- nodes    : V2Ray VMess 节点
- sessions : 抓包会话历史（元数据，非流量本体）
- settings : KV 配置（当前激活节点、抓包端口等）

设计：
- WAL 模式，gevent 下多协程读写更稳。
- 每次操作短连接（check_same_thread=False + 单锁），避免长连接跨协程问题。
- 流量本体（flows）不落库，走内存环形缓冲 + WebSocket 实时推（见 ws.py / capture）。
"""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    protocol    TEXT    NOT NULL DEFAULT 'vmess',
    address     TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    uuid        TEXT    NOT NULL DEFAULT '',
    alter_id    INTEGER NOT NULL DEFAULT 0,
    security    TEXT    NOT NULL DEFAULT 'auto',
    network     TEXT    NOT NULL DEFAULT 'tcp',
    ws_path     TEXT    NOT NULL DEFAULT '',
    ws_host     TEXT    NOT NULL DEFAULT '',
    tls         INTEGER NOT NULL DEFAULT 0,
    sni         TEXT    NOT NULL DEFAULT '',
    remark      TEXT    NOT NULL DEFAULT '',
    raw         TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    stopped_at  INTEGER,
    scope       TEXT    NOT NULL DEFAULT 'global-80-443',
    flow_count  INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init(self) -> None:
        with _LOCK, self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ---------- nodes ----------
    def list_nodes(self) -> List[Dict[str, Any]]:
        with _LOCK, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        with _LOCK, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM nodes WHERE id=?", (node_id,)
            ).fetchone()
        return dict(row) if row else None

    def insert_node(self, node: Dict[str, Any]) -> int:
        now = int(time.time())
        fields = (
            "name", "protocol", "address", "port", "uuid", "alter_id",
            "security", "network", "ws_path", "ws_host", "tls", "sni",
            "remark", "raw",
        )
        vals = [node.get(f, _DEFAULTS[f]) for f in fields]
        with _LOCK, self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO nodes ({','.join(fields)},created_at,updated_at) "
                f"VALUES ({','.join('?' * len(fields))},?,?)",
                (*vals, now, now),
            )
            return cur.lastrowid

    def update_node(self, node_id: int, node: Dict[str, Any]) -> bool:
        now = int(time.time())
        allowed = (
            "name", "protocol", "address", "port", "uuid", "alter_id",
            "security", "network", "ws_path", "ws_host", "tls", "sni",
            "remark", "raw",
        )
        sets = [f for f in allowed if f in node]
        if not sets:
            return False
        assigns = ",".join(f"{f}=?" for f in sets) + ",updated_at=?"
        vals = [node[f] for f in sets] + [now, node_id]
        with _LOCK, self._conn() as conn:
            cur = conn.execute(
                f"UPDATE nodes SET {assigns} WHERE id=?", vals
            )
            return cur.rowcount > 0

    def delete_node(self, node_id: int) -> bool:
        with _LOCK, self._conn() as conn:
            cur = conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            return cur.rowcount > 0

    # ---------- sessions ----------
    def start_session(self, scope: str) -> int:
        now = int(time.time())
        with _LOCK, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (started_at, scope) VALUES (?, ?)",
                (now, scope),
            )
            return cur.lastrowid

    def stop_session(self, session_id: int, flow_count: int) -> None:
        now = int(time.time())
        with _LOCK, self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET stopped_at=?, flow_count=? WHERE id=?",
                (now, flow_count, session_id),
            )

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with _LOCK, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- settings ----------
    def get_setting(self, key: str, default: Any = None) -> Any:
        with _LOCK, self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value)
        with _LOCK, self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )


_DEFAULTS = {
    "name": "node",
    "protocol": "vmess",
    "address": "",
    "port": 0,
    "uuid": "",
    "alter_id": 0,
    "security": "auto",
    "network": "tcp",
    "ws_path": "",
    "ws_host": "",
    "tls": 0,
    "sni": "",
    "remark": "",
    "raw": "",
}


_INSTANCE: Optional[DB] = None


def init_db(path: str) -> DB:
    global _INSTANCE
    _INSTANCE = DB(path)
    return _INSTANCE


def get_db() -> DB:
    if _INSTANCE is None:
        raise RuntimeError("db not initialized")
    return _INSTANCE
