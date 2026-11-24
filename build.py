#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py — 把抓取数据 + 分路名单 注入模板, 生成单文件成品 index.html

输入:
    template.html        HTML 骨架(含占位符)
    app.js               前端逻辑
    champions.json       Data Dragon 英雄映射(scrape.py 生成)
    counter_data.json    op.gg 克制数据(scrape.py 生成)
    build_data.json      op.gg 符文/出装/加点/连招(scrape.py 生成)
    tier_data.json       op.gg 分路排名(scrape.py 生成)
    meta.json            物品/召唤师技能/符文图标映射(scrape.py 生成)
    lanes.txt            用户的正常分路英雄名单(中文名/称号)

输出:
    index.html           可直接双击打开的单文件成品
"""

import json
import os
import re
import sys

try:  # 连招文本归一化（同目录模块），缺失时降级为不处理
    import combo_text as ct
except ImportError:  # pragma: no cover
    ct = None

HERE = os.path.dirname(os.path.abspath(__file__))

TEMPLATE = os.path.join(HERE, "template.html")
APPJS = os.path.join(HERE, "app.js")
CHAMPS = os.path.join(HERE, "champions.json")
COUNTER = os.path.join(HERE, "counter_data.json")
BUILD = os.path.join(HERE, "build_data.json")
TIER = os.path.join(HERE, "tier_data.json")
META = os.path.join(HERE, "meta.json")
LANES = os.path.join(HERE, "lanes.txt")
OUT = os.path.join(HERE, "index.html")

# op.gg 官方头像 CDN。{id} 会被替换为英雄英文 id(大小写敏感)。
ICON_BASE = "https://opgg-static.akamaized.net/meta/images/lol/latest/champion/{id}.png"

ROLE_HEADERS = {
    "上路": "top", "打野": "jungle", "中路": "mid",
    "下路": "adc", "辅助": "support",
}


def load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def normalize_build_combos(data):
    """出成品前统一清洗连招：AA/auto attack -> 平A、去掉英文括注、箭头统一。

    返回被丢弃的空连招条数。
    """
    if ct is None or not data:
        return 0
    dropped = 0
    for role, heroes in data.items():
        for slug, node in (heroes or {}).items():
            if not isinstance(node, dict):
                continue
            combos = node.get("combos") or []
            if not combos:
                continue
            cleaned = ct.normalize_combos(combos)
            dropped += max(0, len(combos) - len(cleaned))
            node["combos"] = cleaned
    return dropped


def load_json_opt(path, label):
    """加载 JSON, 若文件不存在返回 None 并打印提示。"""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            print(f"  [+] 已加载 {label} ({os.path.getsize(path)} 字节)")
            return json.load(f)
    else:
        print(f"  [!] 未找到 {label} ({path}) — 相关功能将降级显示")
        return None


def norm(s):
    """规范化中文名: 去空白/括号/标点。"""
    s = re.sub(r"[\s·•．.、,，]", "", s)
    return s.strip()


def _split_hero_names(line):
    """把行按 "1.xxx 2.xxx 3.xxx" 格式拆成多个英雄名称片段。"""
    # 先按序号拆分: 数字+分隔符(. 或 、)
    parts = re.split(r'\d+\s*[\.\)、]\s*', line)
    # 第一个 part 可能为空或前导文字
    result = []
    for p in parts:
        p = p.strip()
        if p:
            result.append(p)
    return result


def _match_hero(name_str, name_idx, slug_idx, id_idx):
    """尝试匹配一个英雄名称字符串。返回 champ dict 或 None。"""
    # 行尾强制指定 #slug
    forced = None
    mforce = re.search(r"#([A-Za-z]+)\s*$", name_str)
    if mforce:
        forced = mforce.group(1).lower()
        name_str = name_str[:mforce.start()].strip()

    names = []
    mb = re.search(r"[（(]([^）)]+)[）)]", name_str)
    if mb:
        names.append(mb.group(1))                      # 括号内(常为本名)
        names.append(re.sub(r"[（(][^）)]*[）)]", "", name_str))  # 括号外(常为称号)
    else:
        names.append(name_str)

    if forced:
        c = slug_idx.get(forced) or id_idx.get(forced)
        if c:
            return c

    for nm in names:
        c = name_idx.get(norm(nm))
        if c:
            return c
    return None


def parse_lanes(text, champs):
    """解析名单 -> {role: set(champion_key)}。返回 (pool, unmatched)。"""
    # 去除 UTF-8 BOM
    text = text.lstrip("﻿")
    name_idx = {}
    for c in champs:
        for alias in (c["name"], c["title"]):
            if alias:
                name_idx[norm(alias)] = c
    slug_idx = {c["slug"]: c for c in champs}
    id_idx = {c["id"].lower(): c for c in champs}

    pool = {r: set() for r in ROLE_HEADERS.values()}
    unmatched = {r: [] for r in ROLE_HEADERS.values()}
    cur_role = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hdr = line.rstrip("：:").strip()
        if hdr in ROLE_HEADERS:
            cur_role = ROLE_HEADERS[hdr]
            continue
        if cur_role is None:
            continue
        if any(bad in line for bad in ("对比", "图片", "去重", "出现", "总结", "我来", "其中")):
            continue

        # 拆成多个英雄名称片段(处理单行多个英雄的情况)
        fragments = _split_hero_names(line)
        if not fragments:
            continue

        for frag in fragments:
            matched = _match_hero(frag, name_idx, slug_idx, id_idx)
            if matched:
                pool[cur_role].add(matched["key"])
            else:
                unmatched[cur_role].append(frag)

    return pool, unmatched


def main():
    missing_files = [p for p in (TEMPLATE, APPJS, CHAMPS) if not os.path.exists(p)]
    if missing_files:
        sys.exit("缺少文件: " + ", ".join(missing_files) +
                 "\n(champions.json 由 scrape.py 生成; 请先运行 scrape.py)")

    champs = json.loads(load(CHAMPS))
    print(f"[*] 已加载 {len(champs)} 个英雄")

    # 加载各类数据 (均可缺失, 缺失时前端降级)
    print("\n[*] 加载数据文件:")
    counter = load_json_opt(COUNTER, "counter_data.json") or {}
    build_data = load_json_opt(BUILD, "build_data.json") or {}
    tier_data = load_json_opt(TIER, "tier_data.json") or {}
    meta = load_json_opt(META, "meta.json") or {}

    if build_data:
        dropped = normalize_build_combos(build_data)
        print(f"  [*] 连招文本已归一化（AA/auto attack -> 平A、英文括注清理）"
              + (f"，丢弃空连招 {dropped} 条" if dropped else ""))

    if os.path.exists(LANES):
        pool, unmatched = parse_lanes(load(LANES), champs)
    else:
        print("[!] 未找到 lanes.txt, 分路池为空")
        pool, unmatched = {r: set() for r in ROLE_HEADERS.values()}, {}

    # 报告匹配情况
    print("\n=== 分路名单匹配结果 ===")
    for role, keys in pool.items():
        print(f"  {role:8s}: 匹配 {len(keys)} 个英雄")
    total_un = sum(len(v) for v in unmatched.values())
    if total_un:
        print(f"\n[!] 有 {total_un} 个条目未匹配上(已忽略):")
        for role, items in unmatched.items():
            for it in items:
                print(f"    [{role}] {it}")

    lane_pool = {role: sorted(keys) for role, keys in pool.items()}

    # ---- 注入模板 ----
    html = load(TEMPLATE)
    appjs = load(APPJS)

    # 内联 JS
    html = html.replace('<script src="app.js"></script>',
                        "<script>\n" + appjs + "\n</script>")

    # 注入数据占位符
    html = html.replace("__CHAMPIONS__", json.dumps(champs, ensure_ascii=False))
    html = html.replace("__COUNTER__", json.dumps(counter, ensure_ascii=False))
    html = html.replace("__BUILD__", json.dumps(build_data, ensure_ascii=False))
    html = html.replace("__TIER__", json.dumps(tier_data, ensure_ascii=False))
    html = html.replace("__META__", json.dumps(meta, ensure_ascii=False))
    html = html.replace("__LANE_POOL__", json.dumps(lane_pool, ensure_ascii=False))
    html = html.replace("__ICON_BASE__", ICON_BASE)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(OUT) / 1024
    print(f"\n[+] 已生成 {OUT}  ({size_kb:.0f} KB)")
    print("    双击打开即可使用。")


if __name__ == "__main__":
    main()
