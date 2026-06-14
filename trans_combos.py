#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量翻译连招为中文 (sequence + note)。

用法:
    python trans_combos.py          # 翻译所有非中文连招
    python trans_combos.py --dry    # 预览
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_FILE = os.path.join(HERE, "build_data.json")

API_URL = "https://api.deepseek.com/anthropic/v1/messages"
API_KEY = "sk-fc2ccfdf96c84519bac7ec5a56bd087b"
MODEL = "deepseek-v4-pro"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def has_chinese(text):
    return bool(re.search(r'[一-鿿]', text))


def translate_combos(combos):
    """调用 API 翻译连招。只翻译非中文的 sequence 和 note。"""
    # 构建需要翻译的内容
    items = []
    for i, c in enumerate(combos):
        seq_needs = not has_chinese(c.get("sequence", ""))
        note_needs = c.get("note", "") and not has_chinese(c.get("note", ""))
        if seq_needs or note_needs:
            items.append({
                "i": i,
                "seq": c["sequence"],
                "note": c.get("note", ""),
                "seq_tr": seq_needs,
                "note_tr": note_needs,
            })

    if not items:
        return combos  # 已经全是中文

    # 构建 prompt
    lines = []
    for it in items:
        parts = []
        if it["seq_tr"]:
            parts.append(f'seq: "{it["seq"]}"')
        if it["note_tr"]:
            parts.append(f'note: "{it["note"]}"')
        lines.append(f'  {{{", ".join(parts)}}}')
    prompt = f"""Translate these League of Legends combo sequences and notes to 简体中文 (Simplified Chinese, NOT Traditional Chinese). Keep QWERDF keys and arrow (>) symbols unchanged. Only translate the descriptive text.

{chr(10).join(lines)}

Return ONLY this JSON:
{{"translated": [{{"seq": "translated sequence", "note": "translated note"}}, ...]}}"""

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
    }).encode()

    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", API_KEY)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        # 提取 text
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        # 解析 JSON
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            translated = json.loads(m.group(0)).get("translated", [])
            for idx, tr in enumerate(translated):
                if idx < len(items):
                    orig = items[idx]
                    if orig["seq_tr"] and tr.get("seq"):
                        combos[orig["i"]]["sequence"] = tr["seq"]
                    if orig["note_tr"] and tr.get("note"):
                        combos[orig["i"]]["note"] = tr["note"]
        return combos
    except Exception as e:
        print(f"    [ERR] translate: {e}")
        return combos


def main():
    dry_run = "--dry" in sys.argv

    build = load_json(BUILD_FILE)

    # 收集需要翻译的
    tasks = []
    for role in build:
        for slug, data in build[role].items():
            combos = data.get("combos", [])
            needs_tr = False
            for c in combos:
                if not has_chinese(c.get("sequence", "")) or \
                   (c.get("note", "") and not has_chinese(c.get("note", ""))):
                    needs_tr = True
                    break
            if needs_tr:
                tasks.append((role, slug, combos))

    print(f"Need translation: {len(tasks)} heroes")
    if dry_run:
        for role, slug, combos in tasks[:5]:
            for c in combos:
                print(f"  [{role}] {slug}: {c['sequence'][:50]}")
        return

    success = 0
    for i, (role, slug, combos) in enumerate(tasks):
        tag = f"[{i+1}/{len(tasks)}]"
        print(f"{tag} [{role}] {slug} ...", end=" ", flush=True)

        translated = translate_combos(combos)
        build[role][slug]["combos"] = translated
        success += 1

        # 显示翻译结果
        for c in translated[:2]:
            print(f"  {c['sequence'][:40]}", end="")
        print()

        if (i + 1) % 30 == 0:
            save_json(build, BUILD_FILE)
            print(f"  [SAVE] {i+1}/{len(tasks)}")

        time.sleep(0.6)

    save_json(build, BUILD_FILE)
    print(f"\nDone: {success} translated")
    print(f"Saved to {BUILD_FILE}")


if __name__ == "__main__":
    main()
