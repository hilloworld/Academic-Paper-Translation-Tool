"""本地假的「OpenAI 兼容」模型服务，用于不联网、不消耗额度地验证整条翻译链路。

用途：在没有 API Key / 不方便联网时，仍能完整跑通「上传 → 解析 → 翻译 → 对照」流程。

启动：
    uv run python tools/fake_llm.py --port 8799

再把本项目的配置指向它（.env）：
    LLM_PROVIDER=custom
    LLM_BASE_URL=http://127.0.0.1:8799/v1
    LLM_MODEL=fake-translator
    LLM_API_KEY=sk-fake
    LLM_MOCK=0

它会按请求里的片段 id 原样回填一条伪译文（前缀「【伪译】」），
并沿用真实的 JSON 协议，因此足以验证：请求构造 / Bearer 鉴权 / 分块 /
并发 / JSON 解析 / 译文回填 / 缓存落库。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "【伪译】"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeLLM/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", "/v1", "/health"):
            return self._json(200, {"ok": True, "service": "fake-llm", "endpoints": ["/v1/chat/completions"]})
        return self._json(404, {"error": {"message": f"未知路径 {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self.path.endswith("/chat/completions"):
            return self._json(404, {"error": {"message": f"未知路径 {self.path}，请使用 /v1/chat/completions"}})
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            return self._json(401, {"error": {"message": "Missing or invalid Authorization header."}})
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "请求体不是合法 JSON"}})

        messages = body.get("messages") or []
        content = str(messages[-1].get("content") if messages else "")
        try:
            payload = json.loads(content.split("\n\n")[0])
            segments = payload.get("segments") or []
        except Exception:  # noqa: BLE001
            return self._json(
                400,
                {"error": {"message": "未能在 user 消息中解析出 segments 数组（本项目约定为 JSON 载荷）"}},
            )

        items = [{"id": s.get("id"), "text": PREFIX + str(s.get("text", ""))} for s in segments]
        reply = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": body.get("model") or "fake-translator",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps({"translations": items}, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return self._json(200, reply)

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args) -> None:  # noqa: ANN002
        print(f"[fake-llm] {fmt % args}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="本地假 LLM 服务（OpenAI 兼容）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"假 LLM 服务已启动：http://{args.host}:{args.port}/v1  （Ctrl+C 停止）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
