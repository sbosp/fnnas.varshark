"""WebSocket 实时推送 hub + 流量环形缓冲。

- Hub：管理所有已连接的 ws 客户端，broadcast() 向全部在线客户端推 JSON。
- FlowBuffer：内存环形缓冲，保存最近 N 条 flow（抓包流量本体不落库）。
  新客户端连上先补发缓冲内的历史，再持续收增量。

抓包 addon 通过 publish_flow() 投递事件；proxy/capture 状态变化通过
publish_event() 投递。前端单条 ws 连接即可收到全部推送。

依赖 gevent-websocket（geventwebsocket），随 gevent 一起用。
"""
import collections
import json
import logging
import threading
import time
from typing import Any, Deque, Dict, List, Optional

log = logging.getLogger("rayshark.ws")

_LOCK = threading.RLock()


class FlowBuffer:
    """最近 N 条 flow 的环形缓冲。"""

    def __init__(self, maxlen: int = 1000):
        self._buf: Deque[Dict[str, Any]] = collections.deque(maxlen=maxlen)
        self._seq = 0

    def add(self, flow: Dict[str, Any]) -> Dict[str, Any]:
        with _LOCK:
            self._seq += 1
            flow = dict(flow)
            flow["seq"] = self._seq
            self._buf.append(flow)
            return flow

    def snapshot(self, since_seq: int = 0) -> List[Dict[str, Any]]:
        with _LOCK:
            return [f for f in self._buf if f.get("seq", 0) > since_seq]

    def clear(self) -> None:
        with _LOCK:
            self._buf.clear()

    def count(self) -> int:
        with _LOCK:
            return len(self._buf)


class Hub:
    """管理 ws 客户端并广播消息。"""

    def __init__(self):
        self._clients: set = set()

    def register(self, ws) -> None:
        with _LOCK:
            self._clients.add(ws)
        log.info("ws client connected, total=%d", len(self._clients))

    def unregister(self, ws) -> None:
        with _LOCK:
            self._clients.discard(ws)
        log.info("ws client disconnected, total=%d", len(self._clients))

    def broadcast(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False)
        dead = []
        with _LOCK:
            clients = list(self._clients)
        for ws in clients:
            try:
                ws.send(data)
            except Exception:  # noqa: BLE001 客户端断开
                dead.append(ws)
        if dead:
            with _LOCK:
                for ws in dead:
                    self._clients.discard(ws)

    def client_count(self) -> int:
        with _LOCK:
            return len(self._clients)


_hub: Optional[Hub] = None
_flows: Optional[FlowBuffer] = None


def init_ws(flow_buffer_size: int = 1000) -> None:
    global _hub, _flows
    _hub = Hub()
    _flows = FlowBuffer(maxlen=flow_buffer_size)


def get_hub() -> Hub:
    if _hub is None:
        init_ws()
    return _hub  # type: ignore[return-value]


def get_flows() -> FlowBuffer:
    if _flows is None:
        init_ws()
    return _flows  # type: ignore[return-value]


def publish_flow(flow: Dict[str, Any]) -> None:
    """抓包 addon 调用：投递一条流量并广播。"""
    stored = get_flows().add(flow)
    get_hub().broadcast({"type": "flow", "data": stored})


def publish_event(event: str, payload: Dict[str, Any]) -> None:
    """状态变化（代理/抓包启停等）广播。"""
    get_hub().broadcast({"type": "event", "event": event,
                         "data": payload, "ts": int(time.time())})
