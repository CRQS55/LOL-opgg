#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_common.py — DeepSeek Anthropic 兼容接口的公共封装（gen_combos / trans_combos 共用）。

为什么需要它:
  * 账号实际可用的模型名只有 deepseek-flash / deepseek-v4-pro；
    写错模型名会被 400 拒绝，旧代码只打印 "HTTP Error 400: Bad Request"，
    看不出原因，还会继续跑 193 次。
  * 这里把错误分类: 模型名错 -> 自动换下一个模型；限流/服务端 -> 退避重试；
    鉴权错 -> 立刻停下（不浪费请求）；其它 4xx -> 带出 API 返回体。
  * 返回体统一拼成纯文本，调用方再按需解析 JSON。
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_API_URL = "https://api.deepseek.com/anthropic/v1/messages"
# 2026-11 实测该账号只接受这两个模型名（GET /models 返回的就是它们）
MODEL_CHAIN = ("deepseek-flash", "deepseek-v4-pro")
DEFAULT_MAX_TOKENS = 800
DEFAULT_TIMEOUT = 90
DEFAULT_RETRIES = 3
DEFAULT_CONCURRENCY = 16
MAX_CONCURRENCY = 800      # 上限（英雄最多 193 个，实际会按任务数收敛）


class AiError(Exception):
    """kind: auth | model | rate | server | bad_request | other | all_failed"""

    def __init__(self, kind, message, status=None, body=""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.body = body or ""

    def __str__(self):
        if self.status:
            return f"[{self.kind}] HTTP {self.status}: {self.message}"
        return f"[{self.kind}] {self.message}"


# ======================== 纯函数（便于穷举验证） ========================
def classify_http_error(status, body):
    """把 HTTP 错误分类成: auth / model / rate / server / bad_request / other。"""
    b = (body or "").lower()
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate"
    if status >= 500:
        return "server"
    if status == 400:
        if ("model name" in b or "supported api model" in b
                or "model not found" in b or "invalid model" in b
                or "模型" in body):
            return "model"
        return "bad_request"
    return "other"


def backoff_delay(attempt, rng=None, base=1.0, cap=15.0):
    r = rng or random
    d = min(cap, base * (2 ** max(0, attempt - 1)))
    jitter = r.uniform(0, min(0.5, d * 0.3))
    return round(min(cap, d + jitter), 2)


def extract_json_object(text):
    """从模型输出里抠出第一个完整的 JSON 对象（字符串感知的括号配对）。"""
    if not text:
        return None
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def message_text(data):
    """把 anthropic 响应里的 text/thinking block 拼成纯文本。"""
    parts = []
    for b in (data or {}).get("content", []) or []:
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
    if not "".join(parts).strip():
        for b in (data or {}).get("content", []) or []:
            if b.get("type") == "thinking":
                parts.append(b.get("thinking", ""))
    return "".join(parts)


# ======================== 文件/配置 ========================
def load_api_key(here):
    env_file = os.path.join(here, ".env")
    if os.path.isfile(env_file):
        try:
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def mask_key(key):
    if not key or len(key) < 12:
        return "(未配置)"
    return key[:5] + "..." + key[-4:]


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(data, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ======================== HTTP ========================
def post_messages(prompt, *, model, api_key, api_url=DEFAULT_API_URL,
                  max_tokens=DEFAULT_MAX_TOKENS, timeout=DEFAULT_TIMEOUT,
                  thinking_disabled=True):
    """发一次 messages 请求，返回解析后的 JSON；失败抛 AiError。"""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if thinking_disabled:
        body["thinking"] = {"type": "disabled"}

    def _do(b):
        req = urllib.request.Request(api_url, data=json.dumps(b).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise AiError(classify_http_error(e.code, raw), raw[:300], status=e.code, body=raw)
        except urllib.error.URLError as e:
            raise AiError("other", f"网络错误: {e.reason}")
        except TimeoutError:
            raise AiError("server", "请求超时")
        except json.JSONDecodeError as e:
            raise AiError("other", f"响应不是合法 JSON: {e}")

    try:
        return _do(body)
    except AiError as e:
        # 个别模型/网关不认 thinking 参数: 去掉再试一次
        if thinking_disabled and e.status == 400 and "thinking" in (e.body or "").lower():
            body.pop("thinking", None)
            return _do(body)
        raise


def request_text(prompt, *, api_key, models=MODEL_CHAIN, api_url=DEFAULT_API_URL,
                 max_tokens=DEFAULT_MAX_TOKENS, retries=DEFAULT_RETRIES,
                 timeout=DEFAULT_TIMEOUT, sleep=time.sleep, log=print):
    """按模型链依次尝试，返回 (text, err, model_used)。

    - 模型名无效(400 model)   -> 换下一个模型
    - 限流(429)/服务端(5xx)   -> 指数退避重试，重试用尽再换模型
    - 鉴权失败(401/403)      -> 立刻返回，交给调用方终止
    """
    reasons = []
    for model in models:
        for attempt in range(1, max(1, retries) + 1):
            try:
                data = post_messages(prompt, model=model, api_key=api_key, api_url=api_url,
                                     max_tokens=max_tokens, timeout=timeout)
                text = message_text(data)
                if not text.strip():
                    raise AiError("other", "响应里没有文本内容")
                return text, None, model
            except AiError as e:
                if e.kind == "auth":
                    return None, e, model
                if e.kind == "model":
                    log(f"    [模型] {model} 不可用（{e.message[:90]}），换下一个模型 ...")
                    reasons.append(f"{model}: 模型名无效")
                    break  # 换模型
                if e.kind in ("rate", "server"):
                    if attempt < retries:
                        d = backoff_delay(attempt)
                        log(f"    [重试] {model} {e.kind} 失败({e.message[:60]})，{d}s 后第 "
                            f"{attempt + 1} 次尝试 ...")
                        sleep(d)
                        continue
                    reasons.append(f"{model}: {e.kind} 连续 {retries} 次失败")
                    break
                reasons.append(f"{model}: {e.message[:80]}")
                break
    return None, AiError("all_failed", " | ".join(reasons) or "没有可用模型"), None


def clamp_workers(concurrency, n_items):
    """并发数收敛: 不超过任务数、不超过 MAX_CONCURRENCY、至少 1。"""
    try:
        c = int(concurrency)
    except (TypeError, ValueError):
        c = DEFAULT_CONCURRENCY
    return max(1, min(c, max(1, int(n_items)), MAX_CONCURRENCY))


def parallel_map(items, worker, concurrency=DEFAULT_CONCURRENCY, on_result=None):
    """线程池并发跑 worker(item)，按完成顺序回调 on_result(idx, item, result)。

    - worker 抛异常时把异常本身作为 result 传回（不让一个坏任务打断整批）
    - on_result 返回 False -> 取消剩余任务并提前结束
    - 返回 (results, aborted)；results 与 items 同序，未执行的为 None
    """
    items = list(items)
    n = len(items)
    if n == 0:
        return [], False
    workers = clamp_workers(concurrency, n)
    results = [None] * n
    aborted = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai") as ex:
        fut_index = {ex.submit(worker, it): i for i, it in enumerate(items)}
        for fut in as_completed(fut_index):
            idx = fut_index[fut]
            try:
                res = fut.result()
            except Exception as e:  # worker 内部异常
                res = AiError("other", f"{e.__class__.__name__}: {e}")
            results[idx] = res
            keep_going = True
            if on_result is not None:
                keep_going = on_result(idx, items[idx], res)
            if keep_going is False:
                aborted = True
                for f in fut_index:
                    f.cancel()
                break
    return results, aborted
