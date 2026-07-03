"""mitmproxy addon —— 在 mitmdump 进程内运行，把每条 flow 回传给后端。

mitmdump 是独立进程，无法直接访问后端内存里的 Hub / FlowBuffer，
因此通过本地回环 HTTP 把精简后的 flow POST 到后端内部接口：
    POST http://127.0.0.1:<RAYSHARK_INGEST_PORT>/_ingest/flow

后端 /_ingest/flow 再 publish_flow() 广播给前端 WebSocket。

只回传元数据 + 截断后的 body（默认 64KB），避免大响应撑爆内存/带宽。

启动方式（capture.py 里）：
    mitmdump --mode transparent -s mitm_addon.py --set ...
"""
import json
import os
import time
import urllib.request

INGEST_URL = os.environ.get(
    "RAYSHARK_INGEST_URL", "http://127.0.0.1:11090/api/_ingest/flow"
)
MAX_BODY = int(os.environ.get("RAYSHARK_MAX_BODY", str(64 * 1024)))


def _safe_text(content: bytes, limit: int = MAX_BODY) -> dict:
    if content is None:
        return {"text": "", "size": 0, "truncated": False}
    size = len(content)
    chunk = content[:limit]
    try:
        text = chunk.decode("utf-8")
        is_text = True
    except UnicodeDecodeError:
        text = ""
        is_text = False
    return {
        "text": text if is_text else "",
        "size": size,
        "truncated": size > limit,
        "binary": not is_text,
    }


def _post(payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        INGEST_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        # 后端可能瞬时不可用，丢弃即可，不能阻塞抓包
        pass


class RaysharkAddon:
    def response(self, flow) -> None:  # noqa: ANN001 mitmproxy Flow
        try:
            req = flow.request
            resp = flow.response
            rec = {
                "id": flow.id,
                "ts": time.time(),
                "method": req.method,
                "scheme": req.scheme,
                "host": req.pretty_host,
                "port": req.port,
                "path": req.path,
                "url": req.pretty_url,
                "http_version": req.http_version,
                "status": resp.status_code if resp else 0,
                "req_headers": dict(req.headers),
                "resp_headers": dict(resp.headers) if resp else {},
                "req_body": _safe_text(req.raw_content or b""),
                "resp_body": _safe_text(resp.raw_content or b"") if resp else None,
                "content_type": resp.headers.get("content-type", "") if resp else "",
                "duration_ms": int(
                    ((resp.timestamp_end or 0) - (req.timestamp_start or 0)) * 1000
                ) if resp else 0,
            }
            _post(rec)
        except Exception:
            pass

    def error(self, flow) -> None:  # noqa: ANN001 连接/TLS 错误也回传
        try:
            req = getattr(flow, "request", None)
            rec = {
                "id": getattr(flow, "id", ""),
                "ts": time.time(),
                "method": req.method if req else "",
                "host": req.pretty_host if req else "",
                "url": req.pretty_url if req else "",
                "status": 0,
                "error": str(flow.error) if getattr(flow, "error", None) else "error",
            }
            _post(rec)
        except Exception:
            pass


addons = [RaysharkAddon()]
