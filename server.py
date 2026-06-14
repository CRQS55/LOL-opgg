#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单文件服务器: 静态文件 + AI API 代理。

一键启动:
    python server.py
    → 浏览器打开 http://localhost:8899

无需额外启动 proxy.py 或 python -m http.server。
"""

import json
import os
import sys
import urllib.request
import urllib.error
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler

# ====== 配置 ======
PORT = 8899
AI_TARGET = "https://api.deepseek.com/anthropic"  # DeepSeek API (Anthropic 兼容)
API_PREFIX = "/v1/"                                # 以该前缀开头的请求走代理
DIR = os.path.dirname(os.path.abspath(__file__))   # 静态文件根目录

# 从 .env 读取 API Key（不硬编码，不暴露给前端）
def _load_api_key():
    env_file = os.path.join(DIR, ".env")
    if os.path.isfile(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1]
    return None

AI_KEY = _load_api_key()

# 常见 MIME 补充
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


class UnifiedHandler(BaseHTTPRequestHandler):
    """统一处理器: API 请求走代理, 其他走静态文件"""

    # ====== 静态文件服务 ======
    def do_GET(self):
        """GET: 静态文件 / index.html"""
        path = self.path.split("?")[0]  # 去 query string
        if path == "/":
            path = "/index.html"

        filepath = os.path.join(DIR, path.lstrip("/"))
        if not os.path.abspath(filepath).startswith(DIR):
            self.send_error(403)
            return

        if not os.path.isfile(filepath):
            self.send_error(404)
            return

        mime, _ = mimetypes.guess_type(filepath)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    # ====== API 代理 ======
    def do_OPTIONS(self):
        """CORS 预检"""
        if self.path.startswith(API_PREFIX):
            self.send_response(204)
            self._cors()
            self.end_headers()
        else:
            self.send_error(405)

    def do_POST(self):
        """POST: 代理到 AI API"""
        if not self.path.startswith(API_PREFIX):
            self.send_error(404, "API only")
            return

        try:
            body_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(body_len) if body_len else b"{}"

            req = urllib.request.Request(
                AI_TARGET + self.path,
                data=body,
                method="POST",
            )
            # 转发必要的请求头（但不转发客户端发来的 API Key，用服务端 Key 替代）
            for k, v in self.headers.items():
                low = k.lower()
                if low in ("host", "origin", "referer", "x-api-key"):
                    continue
                req.add_header(k, v)
            # 注入服务端持有的 API Key
            if AI_KEY:
                req.add_header("x-api-key", AI_KEY)
            req.add_header("anthropic-version", "2023-06-01")

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
        line = args[0] if args else ""
        # 从请求行提取路径 (格式: "METHOD /path HTTP/1.1")
        parts = str(line).split()
        path = parts[1] if len(parts) >= 2 else ""
        tag = "[api]" if path.startswith(API_PREFIX) else "[static]"
        print(f"{tag} {line}")


if __name__ == "__main__":
    import webbrowser
    from threading import Timer

    print(f"⚡ 统一服务器启动")
    print(f"   静态文件: {DIR}")
    print(f"   API 代理: localhost:{PORT}{API_PREFIX}* → {AI_TARGET}")
    if not AI_KEY:
        print(f"   ⚠ 未配置 DEEPSEEK_API_KEY（请检查 .env 文件），AI 功能将不可用")
    url = f"http://localhost:{PORT}"
    print(f"   打开浏览器: {url}")
    print(f"   按 Ctrl+C 停止\n")

    # 自动打开浏览器 (延迟 0.5s 等服务器就绪)
    Timer(0.5, lambda: webbrowser.open(url)).start()

    server = HTTPServer(("127.0.0.1", PORT), UnifiedHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
