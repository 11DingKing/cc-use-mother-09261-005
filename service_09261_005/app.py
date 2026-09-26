"""本地 HTTP 服务入口，仅依赖 Python 标准库。

启动：python3 -m service_09261_005.app [--db trial.db] [--port 8000]
健康检查：GET /health
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from .api import dispatch
from .store import SQLiteStore
from .workflow import Workflow

FLOW = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        parts = urlsplit(self.path)
        if parts.path == "/health":
            self._send(200, {"status": "ok"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid_json", "message": "请求体不是合法 JSON"})
            return
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        status, payload = dispatch(FLOW, method, parts.path, body, query)
        self._send(status, payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        pass  # 静默；生产环境可按需接入日志


def build_flow(db_path):
    return Workflow(SQLiteStore(db_path))


def main(argv=None):
    global FLOW
    parser = argparse.ArgumentParser(description="教材试用观察期服务")
    parser.add_argument("--db", default="trial.db", help="SQLite 文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    FLOW = build_flow(args.db)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"教材试用观察期服务已启动：http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        FLOW.store.close()


if __name__ == "__main__":
    main()
