#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量把连招翻译成简体中文（sequence + note）— 并发版。

用法:
    python trans_combos.py                # 并发翻译所有非中文连招（默认并发 16）
    python trans_combos.py --concurrency 32
    python trans_combos.py --dry          # 预览
    python trans_combos.py --limit 5      # 只翻译前 5 个英雄
    python trans_combos.py --data-dir out

说明:
    与 gen_combos.py 共用 ai_common，模型名自动降级（deepseek-flash -> deepseek-v4-pro），
    失败会带出 API 返回体，且不会再把"没翻译成功"算成成功。
    193 个英雄实测: 串行旧版 461s -> 并发 8: 42s。
"""

import argparse
import os
import re
import sys
import threading
import time

import ai_common as ai
import combo_text as ct

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_FILE = os.path.join(HERE, "build_data.json")


def has_chinese(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def build_prompt(items):
    lines = []
    for it in items:
        parts = []
        if it["seq_tr"]:
            parts.append(f'seq: "{it["seq"]}"')
        if it["note_tr"]:
            parts.append(f'note: "{it["note"]}"')
        lines.append(f'  {{{", ".join(parts)}}}')
    return f"""Translate everything below into 简体中文 (Simplified Chinese, NOT Traditional Chinese).
Rules:
1. Translate ALL descriptive text AND champion ability names into their official 中文 skill names (e.g. "Condemn" -> "恶魔审判", "Tumble" -> "闪避突袭").
2. Do NOT leave English words in the output. Only keep champion keys (Q/W/E/R/D/F), digits and the arrow symbol (>).
3. Keep the number and order of items exactly the same.

Input:
{chr(10).join(lines)}

Return ONLY this JSON:
{{"translated": [{{"seq": "translated sequence", "note": "translated note"}}, ...]}}"""


def collect_items(combos):
    """找出需要翻译的条目。

    - sequence: 只在还残留英文单词（AA/auto/kite…）时才需要处理；纯键位/中文不需要
    - note: 没有中文，或英文占比过高（模型跑偏/英文技能名过多）时需要重新翻译
    """
    items = []
    for i, c in enumerate(combos or []):
        seq = c.get("sequence", "") or ""
        note = c.get("note", "") or ""
        seq_needs = bool(seq) and ct.has_english_leftover(seq)
        note_needs = bool(note) and (not has_chinese(note) or ct.has_heavy_english(note))
        if seq_needs or note_needs:
            items.append({"i": i, "seq": seq, "note": note,
                          "seq_tr": seq_needs, "note_tr": note_needs})
    return items


def apply_translation(combos, items, obj):
    """把 API 返回的 translated 列表写回 combos，返回实际改动的条数。"""
    if not isinstance(obj, dict):
        return 0
    translated = obj.get("translated")
    if not isinstance(translated, list):
        return 0
    changed = 0
    for idx, tr in enumerate(translated):
        if idx >= len(items) or not isinstance(tr, dict):
            continue
        orig = items[idx]
        if orig["seq_tr"] and tr.get("seq"):
            combos[orig["i"]]["sequence"] = tr["seq"]
            changed += 1
        if orig["note_tr"] and tr.get("note"):
            combos[orig["i"]]["note"] = tr["note"]
            changed += 1
    return changed


def translate_combos(combos, *, api_key, models, api_url, retries, log=print):
    """调用 API 翻译。返回 (combos, status, err, model)。

    status: unchanged | translated | parse_fail | api_fail | auth
    """
    items = collect_items(combos)
    if not items:
        return ct.normalize_combos(combos), "unchanged", None, None

    text, err, model = ai.request_text(build_prompt(items), api_key=api_key, models=models,
                                       api_url=api_url, max_tokens=1200, retries=retries,
                                       log=log)
    if err is not None:
        return combos, ("auth" if err.kind == "auth" else "api_fail"), err, model

    obj = ai.extract_json_object(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("translated"), list):
        return combos, "parse_fail", None, model
    changed = apply_translation(combos, items, obj)
    # 翻译后再做一次文本归一化（去掉英文括注/英文碎片），保证前端只看到中文
    combos = ct.normalize_combos(combos)
    return combos, ("translated" if changed else "parse_fail"), None, model


def build_parser():
    ap = argparse.ArgumentParser(description="批量翻译连招为中文（并发）")
    ap.add_argument("--dry", action="store_true", help="只预览，不调用 API")
    ap.add_argument("--normalize", action="store_true",
                    help="只做本地归一化（AA/auto attack -> 平A、去掉英文括注、箭头统一），不调用 API")
    ap.add_argument("--limit", type=int, help="只处理前 N 个英雄")
    ap.add_argument("--concurrency", type=int, default=ai.DEFAULT_CONCURRENCY,
                    help=f"并发请求数（默认 {ai.DEFAULT_CONCURRENCY}，上限 {ai.MAX_CONCURRENCY}）")
    ap.add_argument("--model", help="指定模型，逗号分隔可给多个（默认 deepseek-flash -> deepseek-v4-pro）")
    ap.add_argument("--api-url", default=ai.DEFAULT_API_URL, help="API 地址（测试用）")
    ap.add_argument("--data-dir", default=HERE, help="数据目录（默认脚本所在目录）")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="每次请求前的额外等待秒数（并发时一般不用；默认 0）")
    ap.add_argument("--retries", type=int, default=3, help="限流/服务端错误的单模型重试次数")
    return ap


def main():
    args = build_parser().parse_args()
    data_dir = os.path.abspath(args.data_dir)
    build_file = os.path.join(data_dir, "build_data.json")
    if not os.path.exists(build_file):
        sys.exit(f"错误: 找不到 {build_file}，请先运行 gen_combos.py")

    # 纯本地归一化：不需要 API Key，也不发任何请求
    if args.normalize:
        build = ai.load_json(build_file)
        stats = {"heroes": 0, "combos": 0, "seq_changed": 0, "note_changed": 0, "dropped": 0}
        for role in build:
            for slug, node in build[role].items():
                combos = node.get("combos") or []
                if not combos:
                    continue
                stats["heroes"] += 1
                cleaned = []
                for c in combos:
                    stats["combos"] += 1
                    nc = ct.normalize_combo(c)
                    if not nc:
                        stats["dropped"] += 1
                        continue
                    if nc["sequence"] != c.get("sequence"):
                        stats["seq_changed"] += 1
                    if nc["note"] != (c.get("note") or ""):
                        stats["note_changed"] += 1
                    cleaned.append(nc)
                node["combos"] = cleaned
        ai.save_json_atomic(build, build_file)
        print(f"[+] 归一化完成: {stats['heroes']} 个英雄 / {stats['combos']} 条连招，"
              f"改写序列 {stats['seq_changed']} · 改写说明 {stats['note_changed']} · "
              f"丢弃空连招 {stats['dropped']}")
        print(f"    写入 {build_file}")
        return 0

    api_key = ai.load_api_key(HERE)
    if not api_key:
        sys.exit("错误: 未找到 DEEPSEEK_API_KEY。请在 .env 文件中配置（参考 .env.example）。")

    models = tuple(s.strip() for s in args.model.split(",") if s.strip()) if args.model \
        else ai.MODEL_CHAIN
    build = ai.load_json(build_file)

    tasks = []
    for role in build:
        for slug, node in build[role].items():
            if collect_items(node.get("combos") or []):
                tasks.append((role, slug))
    if args.limit:
        tasks = tasks[:args.limit]

    n = len(tasks)
    print(f"[*] 待翻译: {n} 个英雄（模型链: {' -> '.join(models)}）")
    if not n:
        print("[*] 连招已经都是中文，无需翻译")
        return 0
    if args.dry:
        for role, slug in tasks[:5]:
            for c in (build[role][slug].get("combos") or [])[:2]:
                print(f"  [{role}] {slug}: {c.get('sequence', '')[:50]}")
        return 0

    workers = ai.clamp_workers(args.concurrency, n)
    print_lock = threading.Lock()

    def say(msg):
        with print_lock:
            print(msg, flush=True)

    stats = {"translated": 0, "unchanged": 0, "parse_fail": 0, "api_fail": 0}
    state = {"done": 0, "api_fail_streak": 0, "auth": False}
    t0 = time.time()

    def worker(task):
        role, slug = task
        ts = time.time()
        combos, status, err, model = translate_combos(
            build[role][slug].get("combos") or [],
            api_key=api_key, models=models, api_url=args.api_url, retries=args.retries,
            log=lambda m: say(f"  [{slug}]{m}"))
        return combos, status, err, model, time.time() - ts

    def on_result(idx, task, res):
        role, slug = task
        if isinstance(res, BaseException):
            combos, status, err, model, elapsed = None, "api_fail", ai.AiError("other", str(res)), None, 0.0
        else:
            combos, status, err, model, elapsed = res

        if combos is not None:
            build[role][slug]["combos"] = combos
        stats[status] = stats.get(status, 0) + 1

        if status == "auth":
            say(f"[{idx + 1}/{n}] [{role}] {slug} 鉴权失败: {err}")
            state["auth"] = True
            return False
        if status == "translated":
            note = (combos[0].get("note", "") if combos else "")[:30]
            say(f"[{idx + 1}/{n}] [{role}] {slug} OK [{model}] {note}  ({elapsed:.1f}s)")
            state["api_fail_streak"] = 0
        elif status == "unchanged":
            say(f"[{idx + 1}/{n}] [{role}] {slug} 跳过（已是中文）")
            state["api_fail_streak"] = 0
        else:
            reason = str(err) if err else "返回内容无法解析成 translated JSON"
            say(f"[{idx + 1}/{n}] [{role}] {slug} {status}（{reason[:90]}）  ({elapsed:.1f}s)")
            state["api_fail_streak"] += 1
            if state["api_fail_streak"] >= max(6, workers) and stats["translated"] == 0:
                say(f"\n[!] 连续 {state['api_fail_streak']} 个都没成功，已停止，"
                    f"已保存部分结果（修好配置后重跑会接着补）")
                return False

        state["done"] += 1
        if state["done"] % 30 == 0:
            ai.save_json_atomic(build, build_file)
            say(f"  [SAVE] {state['done']}/{n}")
        return True

    try:
        _, aborted = ai.parallel_map(tasks, worker, concurrency=workers, on_result=on_result)
    except KeyboardInterrupt:
        print("\n[中断] 保存已翻译的部分 ...")
        ai.save_json_atomic(build, build_file)
        return 130

    ai.save_json_atomic(build, build_file)
    dur = time.time() - t0
    if state["auth"]:
        print("\n[!] 鉴权失败：请检查 .env 里的 DEEPSEEK_API_KEY。已翻译部分已保存。")
        return 2

    print(f"\n[+] 完成: 翻译 {stats['translated']} · 已是中文 {stats['unchanged']} · "
          f"解析失败 {stats['parse_fail']} · 接口失败 {stats['api_fail']}"
          f"（{dur:.1f}s，并发 {workers}）")
    print(f"    写入 {build_file}")
    print("    下一步: python build.py")
    if aborted:
        print("[!] 提前中止，剩余英雄未处理；重跑会接着补。")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
