#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 CORS 代理: 转发浏览器请求到 buzz7.top, 绕过跨域限制。

用法:
    python proxy.py
    → 监听 http://localhost:8899
"""

import json
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

TARGET = "https://api.deepseek.com/anthropic"
PORT = 8899


class ProxyHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        """预检请求"""
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            body_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(body_len) if body_len else b"{}"

            req = urllib.request.Request(
                TARGET + self.path,
                data=body,
                method="POST",
            )
            for k, v in self.headers.items():
                if k.lower() in ("host", "origin", "referer"):
                    continue
                req.add_header(k, v)

            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    self.send_response(resp.status)
                    self._cors()
                    for k, v in resp.headers.items():
                        if k.lower() not in ("transfer-encoding", "content-encoding"):
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except urllib.error.HTTPError as e:
                # 上游返回了错误码(401/503等), 透传给浏览器
                err_body = e.read()
                self.send_response(e.code)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_body)))
                self.end_headers()
                self.wfile.write(err_body)
        except Exception as e:
            self.send_response(502)
            self._cors()
            err = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def log_message(self, format, *args):
        print(f"[proxy] {args[0]}")


if __name__ == "__main__":
    print(f"[proxy] 目标: {TARGET}")
    print(f"[proxy] 监听: http://localhost:{PORT}")
    server = HTTPServer(("127.0.0.1", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] 已停止")
