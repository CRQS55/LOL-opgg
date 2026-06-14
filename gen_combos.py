#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量 AI 生成连招, 填入 build_data.json。

用法:
    python gen_combos.py          # 生成所有缺失的连招
    python gen_combos.py --dry    # 预览不调用 API
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_FILE = os.path.join(HERE, "build_data.json")
CHAMPS_FILE = os.path.join(HERE, "champions.json")

# DeepSeek Anthropic-compatible API
API_URL = "https://api.deepseek.com/anthropic/v1/messages"
API_KEY = "sk-fc2ccfdf96c84519bac7ec5a56bd087b"
MODEL = "deepseek-v4-pro"

ROLE_NAMES = {
    "top": "上路", "jungle": "打野", "mid": "中路", "adc": "下路", "support": "辅助",
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def call_api(champion_name, role, skills, skill_order):
    """调用 DeepSeek API 生成连招。返回 [{type, sequence, note}] 或 None。"""
    role_cn = ROLE_NAMES.get(role, role)
    # 构造英文名锚点, 帮助模型识别正确英雄(避免重做英雄用旧技能)
    champ_id = champion_name  # e.g., "暗黑之女" for Annie
    prompt = f"""You are a League of Legends expert with knowledge of ALL champion reworks up to 2025. Use CURRENT patch abilities.

Champion: {champion_name} (English name may help identify correct abilities)
Role: {role_cn}
Skill max order: {skills}
First 6 levels: {skill_order}

What are the core combos? Reply with ONLY valid JSON, no markdown, no other text:
{{"combos":[{{"type":"basic","sequence":"key sequence here","note":"when to use"}},{{"type":"medium","sequence":"key sequence here","note":"when to use"}}]}}"""

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 350,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
    }).encode()

    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", API_KEY)
    req.add_header("anthropic-version", "2023-06-01")

    def extract_json(text):
        """从文本中提取 combos JSON, 支持多种格式"""
        import re
        # 尝试1: 直接解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "combos" in parsed:
                return parsed["combos"]
        except Exception:
            pass
        # 尝试2: 正则提取第一个完整 JSON 对象
        for pat in [r'\{[\s\S]*?"combos"[\s\S]*?\}']:
            m = re.search(pat, text)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                    if "combos" in parsed:
                        return parsed["combos"]
                except Exception:
                    # 可能是嵌套 JSON，尝试找最外层
                    pass
        # 尝试3: 找平衡的 { } 对
        start = text.find('{')
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '{': depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(text[start:i+1])
                            if "combos" in parsed:
                                return parsed["combos"]
                        except Exception:
                            pass
                        break
        return None

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        combos = None
        # 优先从 text block 提取 (更干净)
        text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        for txt in text_blocks:
            combos = extract_json(txt)
            if combos:
                break
        # 回退: 从 thinking block 提取
        if not combos:
            for b in data.get("content", []):
                if b.get("type") == "thinking":
                    combos = extract_json(b.get("thinking", ""))
                    if combos:
                        break
        # 最后: 合并所有内容尝试
        if not combos:
            all_text = "".join(b.get("text", b.get("thinking", "")) for b in data.get("content", []))
            combos = extract_json(all_text)

        if combos:
            result = [{"type": c.get("type", "basic"),
                       "sequence": c.get("sequence", ""),
                       "note": c.get("note", "")} for c in combos]
            # 拒绝明显是模板的连招 (模型没思考直接抄)
            generic_patterns = ["use actual champion", "key sequence here", "use actual"]
            for pat in generic_patterns:
                if any(pat.lower() in c["sequence"].lower() for c in result):
                    return None
            return result

        # Debug: show what we got
        preview = "".join(b.get("text", b.get("thinking", "")) for b in data.get("content", []))[:150]
        print(f"    [WARN] parse fail: {preview}...")
        return None
    except Exception as e:
        print(f"    [ERR] API: {e}")
        return None


def fallback_combo(skills_str):
    """API 失败时从技能优先级生成简单连招"""
    skills = [s.strip() for s in skills_str.split(">") if s.strip()]
    if len(skills) >= 3:
        s = skills
        return [
            {"type": "basic", "sequence": f"{s[0]} > {s[1]} > {s[2]}", "note": "基础技能连招"},
            {"type": "medium", "sequence": f"{s[0]} > {s[1]} > R > {s[0]}", "note": "大招起手进阶连招"},
        ]
    if len(skills) >= 2:
        s = skills
        return [
            {"type": "basic", "sequence": f"{s[0]} > {s[1]}", "note": "基础技能连招"},
        ]
    return None


def main():
    dry_run = "--dry" in sys.argv

    build = load_json(BUILD_FILE)
    champs = load_json(CHAMPS_FILE)
    slug_to_name = {c["slug"]: c["name"] for c in champs}

    # 收集需要生成的条目
    tasks = []
    for role in build:
        for slug, data in build[role].items():
            sm = data.get("skill_master", [])
            if sm and not data.get("combos"):
                name = slug_to_name.get(slug, slug)
                so = "".join(data.get("skill_order", [])[:6])
                tasks.append((role, slug, name, ">".join(sm), so))

    print(f"Need combos: {len(tasks)} heroes")
    if dry_run:
        for role, slug, name, skills, so in tasks[:10]:
            print(f"  [{role}] {name}: skills={skills} order={so}")
        print(f"  ... 共 {len(tasks)} 个")
        return

    success = 0
    fail = 0
    for i, (role, slug, name, skills, so) in enumerate(tasks):
        tag = f"[{i+1}/{len(tasks)}]"
        print(f"{tag} [{role}] {name} skills={skills} ...", end=" ", flush=True)

        combos = call_api(name, role, skills, so)
        if not combos:
            combos = fallback_combo(skills)
            if combos:
                print(f"    [FALLBACK] using derived combo")
        if combos:
            build[role][slug]["combos"] = combos
            success += 1
            seqs = [c["sequence"][:30] for c in combos]
            print(f"OK {seqs}")
        else:
            fail += 1
            print("FAIL")

        # 每 20 个保存一次进度
        if (i + 1) % 20 == 0:
            save_json(build, BUILD_FILE)
            print(f"  [SAVE] progress ({i+1}/{len(tasks)})")

        # 限速: 避免被 API 限流
        time.sleep(0.8)

    # 最终保存
    save_json(build, BUILD_FILE)
    print(f"\nDone: success {success}, fail {fail}")
    print(f"Saved to {BUILD_FILE}")
    print("Next: python build.py")


if __name__ == "__main__":
    main()
