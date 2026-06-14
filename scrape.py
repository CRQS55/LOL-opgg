#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py — 使用 Playwright (真实浏览器) 抓取 op.gg 克制/出装/排名数据。

用法:
    python scrape.py                      # 正常抓取(断点续抓)
    python scrape.py --debug thresh       # 只抓 thresh 并 dump 调试数据
    python scrape.py --limit 3            # 每分路只抓前 3 个英雄(快速验证)
    python scrape.py --only counter       # 只抓 counter
    python scrape.py --only build,tier    # 只抓 build 和 tier
    python scrape.py --fresh              # 忽略进度重新抓

依赖:
    pip install playwright requests
    playwright install chromium
"""

import argparse
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

REGIONS = {"global": "global", "kr": "kr", "cn": "cn"}
TIERS = {"diamond": "diamond", "master": "master", "grandmaster": "grandmaster", "challenger": "challenger"}
ROLES = {"top": "top", "jungle": "jungle", "mid": "mid", "adc": "adc", "support": "support"}

BASE = "https://op.gg/zh-cn/lol/champions/{slug}/counters/{role}"
BUILD_URL = "https://op.gg/zh-cn/lol/champions/{slug}/build/{role}"
TIER_URL = "https://op.gg/zh-cn/lol/champions"

DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/champion.json"
DDRAGON_ITEMS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/item.json"
DDRAGON_SUMMS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/summoner.json"
DDRAGON_RUNES = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/runesReforged.json"
DDRAGON_IMG = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/"
RUNE_IMG = "https://ddragon.leagueoflegends.com/cdn/img/"

OUT_COUNTER = os.path.join(HERE, "counter_data.json")
OUT_CHAMPS = os.path.join(HERE, "champions.json")
OUT_BUILD = os.path.join(HERE, "build_data.json")
OUT_TIER = os.path.join(HERE, "tier_data.json")
OUT_META = os.path.join(HERE, "meta.json")
PROGRESS = os.path.join(HERE, ".scrape_progress.json")

PAGE_TIMEOUT = 45000
POST_LOAD_MS = 5000  # 等待 JS 渲染 (增加等待时间确保数据加载)

_VER = {"v": None}
BOOT_IDS = {3006, 3009, 3020, 3047, 3111, 3117, 3158}
CONSUMABLES = {2003, 2004, 2031, 2032, 2033, 2055, 2138, 2139, 2140, 3340, 3341, 3363, 3364, 3599}


# ======================== Data Dragon ========================
def get_version():
    import requests as req
    if _VER["v"] is None:
        _VER["v"] = req.get(DDRAGON_VERSIONS, timeout=20).json()[0]
    return _VER["v"]


def fetch_champions():
    import requests as req
    print("[*] 获取 Data Dragon 版本 ...")
    ver = get_version()
    print(f"    最新版本: {ver}")
    data = req.get(DDRAGON_CHAMPS.format(ver=ver), timeout=20).json()["data"]
    champs = []
    for _, c in data.items():
        champs.append({
            "key": int(c["key"]),
            "id": c["id"],
            "slug": c["id"].lower(),
            "name": c["name"],
            "title": c["title"],
        })
    champs.sort(key=lambda x: x["key"])
    print(f"    共 {len(champs)} 个英雄")
    return champs


def fetch_meta():
    import requests as req
    ver = get_version()
    print("[*] 下载 物品/召唤师技能/符文 元数据 ...")
    img = DDRAGON_IMG.format(ver=ver)
    meta = {"items": {}, "spells": {}, "runes": {}, "runeTrees": {}}

    items = req.get(DDRAGON_ITEMS.format(ver=ver), timeout=20).json()["data"]
    for iid, it in items.items():
        meta["items"][str(iid)] = {
            "name": it.get("name", iid),
            "icon": img + "item/" + it["image"]["full"],
        }

    summs = req.get(DDRAGON_SUMMS.format(ver=ver), timeout=20).json()["data"]
    for _, s in summs.items():
        meta["spells"][str(s["key"])] = {
            "name": s.get("name", s["id"]),
            "icon": img + "spell/" + s["image"]["full"],
            "id": s["id"],
        }
        meta["spells"][s["id"]] = meta["spells"][str(s["key"])]

    runes = req.get(DDRAGON_RUNES.format(ver=ver), timeout=20).json()
    for tree in runes:
        meta["runeTrees"][str(tree["id"])] = {
            "name": tree.get("name", ""),
            "icon": RUNE_IMG + tree["icon"],
        }
        for slot in tree.get("slots", []):
            for r in slot.get("runes", []):
                meta["runes"][str(r["id"])] = {
                    "name": r.get("name", ""),
                    "icon": RUNE_IMG + r["icon"],
                }
    print(f"    物品 {len(items)} · 召唤师技能 {len(summs)} · 符文 {len(meta['runes'])}")
    return meta


# ======================== Playwright 浏览器 ========================
def launch_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("缺少 playwright, 请先运行:\n  pip install playwright\n  playwright install chromium")

    import glob as _glob
    ms_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright")
    exe = None
    if os.path.isdir(ms_dir):
        candidates = _glob.glob(os.path.join(ms_dir, "chromium-*/chrome-win64/chrome.exe"))
        if candidates:
            exe = max(candidates)

    global _pw, _browser
    _pw = sync_playwright().start()
    kw = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox",
                                      "--disable-blink-features=AutomationControlled"]}
    if exe:
        kw["executable_path"] = exe
    _browser = _pw.chromium.launch(**kw)
    print(f"[*] Playwright 浏览器已启动")
    return _browser


def close_browser():
    try:
        _browser.close()
        _pw.stop()
    except Exception:
        pass


def new_page():
    ctx = _browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="zh-CN",
        viewport={"width": 1920, "height": 1080},
    )
    page = ctx.new_page()
    page.set_default_timeout(PAGE_TIMEOUT)
    return page, ctx


# ======================== OP.GG Counter 抓取 ========================
def scrape_counter_page(page, slug, role, region, tier, champions_by_name=None):
    """抓取 counter 数据。返回 list[{opp:key, win:float, games:int}] 或 (None, error_msg)。"""
    params = f"region={region}&type=ranked&tier={tier}"
    url = BASE.format(slug=slug, role=role) + "?" + params
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(POST_LOAD_MS)
        # 额外等待以确保 React 组件完成客户端渲染
        try:
            page.wait_for_selector('aside li strong, text=数据样本不足', timeout=10000)
        except Exception:
            pass  # 超时也无所谓, 继续尝试 DOM 提取
    except Exception as e:
        return None, f"load err: {e}"

    if champions_by_name is None:
        champions_by_name = {}

    # 检测"数据不足" (仅检查可见文本, 避免误判 script 标签内容)
    try:
        body_text = page.evaluate("() => document.body.innerText")
        no_data_kw = ["数据样本不足", "数据不足"]
        for kw in no_data_kw:
            if kw in body_text:
                return None, "no data (insufficient samples)"
    except Exception:
        pass

    try:
        data = page.evaluate("""() => {
            const main = document.querySelector('main');
            if (!main) return [];
            const aside = main.querySelector('aside');
            const container = aside || main;

            const items = [];
            // 核心方法: 找 <strong> 元素中带百分比的, 向上定位 <li> 容器
            const strongEls = [...container.querySelectorAll('strong')]
                .filter(s => /^\\d+\\.?\\d*%$/.test(s.textContent.trim()));

            const seen = new Set();
            for (const strong of strongEls) {
                let li = strong.closest('li');
                if (!li) {
                    let p = strong.parentElement;
                    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
                        if (p.querySelector('img[alt]')) { li = p; break; }
                    }
                }
                if (!li) continue;

                const img = li.querySelector('img[alt]');
                if (!img || !img.alt || img.alt.length < 2 || img.alt.length > 20) continue;

                const fullText = li.textContent.replace(/\\s+/g, ' ').trim();
                const pct = parseFloat(strong.textContent.trim());
                const numMatch = fullText.match(/(\\d{2,7})\\s*$/);
                const games = numMatch ? parseInt(numMatch[1]) : 0;

                if (!seen.has(img.alt) && games > 0) {
                    seen.add(img.alt);
                    items.push({ name: img.alt, winRate: pct, games: games });
                }
            }

            // 备用方法: 直接匹配所有 li 的文本
            if (!items.length) {
                const lis = [...container.querySelectorAll('li')];
                for (const li of lis) {
                    const img = li.querySelector('img[alt]');
                    const text = li.textContent.trim();
                    const pctMatch = text.match(/(\\d+\\.?\\d*)%/);
                    const numMatch = text.match(/(\\d{3,7})\\s*$/);
                    if (img && img.alt.length > 1 && img.alt.length < 20 && pctMatch && numMatch) {
                        items.push({
                            name: img.alt,
                            winRate: parseFloat(pctMatch[1]),
                            games: parseInt(numMatch[1]),
                        });
                    }
                }
            }
            return items;
        }""")

        if data:
            rows = []
            for item in data:
                opp_key = champions_by_name.get(item.get("name", ""))
                if opp_key:
                    rows.append({
                        "opp": opp_key,
                        "win": float(item["winRate"]),
                        "games": int(item["games"]),
                    })
            if rows:
                return rows, None
            else:
                # 有 DOM 数据但无法匹配到英雄 key
                names_found = [item.get("name", "?") for item in data[:5]]
                return None, f"DOM matched {len(data)} items but no champion keys (names: {names_found})"
    except Exception as e:
        return None, f"DOM err: {e}"

    return None, "no strong/li data elements found"


# ======================== OP.GG Build 抓取 ========================
def scrape_build_page(page, slug, role):
    """抓取 build 页。返回 {runes, spells, skill_order, skill_master, core_items, item4, item5, combos}"""
    url = BUILD_URL.format(slug=slug, role=role)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(POST_LOAD_MS)
    except Exception as e:
        return None, f"page load error: {e}"

    result = {"runes": {}, "spells": [], "skill_order": [], "skill_master": [],
              "core_items": [], "item4": [], "item5": [], "combos": []}

    try:
        data = page.evaluate("""() => {
            const allImgs = [...document.querySelectorAll('img')];
            function getNum(src) {
                const parts = src.split('?')[0].split('/').pop().split('.')[0];
                const m = parts.match(/^(\\d{2,5})$/);
                return m ? parseInt(m[1]) : null;
            }
            // === 符文: 只取第一个(最热门)符文页 ===
            function isSelectedPerk(img) {
                // OP.GG 未选中符文: opacity-50 grayscale 类
                const cls = (img.getAttribute('class') || '') + ' ' +
                    ((img.parentElement?.getAttribute?.('class') || '') + ' ' +
                    (img.parentElement?.parentElement?.getAttribute?.('class') || ''));
                if (/opacity-50|grayscale/.test(cls)) return false;
                return true;
            }
            function getRuneData() {
                // 获取所有符文+碎片图片的 DOM 顺序
                const allImgs = [...document.querySelectorAll('img')];
                const styleImgsWithIdx = [];
                const perkImgsWithIdx = [];  // perks + shards
                for (let i = 0; i < allImgs.length; i++) {
                    const src = allImgs[i].src;
                    if (/\\/perkStyle\\//.test(src)) {
                        const id = getNum(src);
                        if (id) styleImgsWithIdx.push({ idx: i, id, alt: allImgs[i].alt });
                    }
                    // 匹配 /perk/ID.png 和 /perkShard/ID.png
                    if ((/\\/perk\\/\\d+\\.png/.test(src) || /\\/perkShard\\//.test(src)) && !/\\/perkStyle\\//.test(src)) {
                        if (isSelectedPerk(allImgs[i])) {
                            const id = getNum(src);
                            if (id) perkImgsWithIdx.push({ idx: i, id, alt: allImgs[i].alt });
                        }
                    }
                }
                if (styleImgsWithIdx.length < 2) return null;
                const page1Styles = styleImgsWithIdx.slice(0, 2);
                // 分隔线: 第3个 style 的位置 (第二页第一个样式之前)
                const boundary = styleImgsWithIdx.length >= 3
                    ? styleImgsWithIdx[2].idx
                    : 999999;
                const page1Perks = perkImgsWithIdx.filter(p => p.idx < boundary);
                const seen = new Set();
                const perks = page1Perks.filter(p => { if (seen.has(p.id)) return false; seen.add(p.id); return true; });
                return { perks, styles: page1Styles };
            }
            const runeData = getRuneData();
            const perkImgs = runeData ? runeData.perks : allImgs.filter(img => /\\/perk\\/\\d+\\.png/.test(img.src))
                .map(img => ({ alt: img.alt, id: getNum(img.src) }));
            const styleImgs = runeData ? runeData.styles : allImgs.filter(img => /\\/perkStyle\\//.test(img.src))
                .map(img => ({ alt: img.alt, id: getNum(img.src) }));
            // === 装备 ===
            const itemImgs = allImgs.filter(img => /\\/item\\/\\d+\\.png/.test(img.src))
                .map(img => ({ alt: img.alt, id: getNum(img.src) }));
            // 召唤师技能: 搜索文本 "闪现", "点燃" 等关键词附近的图片
            const spellNames = ['闪现', '点燃', '传送', '惩戒', '疾跑', '治疗', '屏障', '虚弱', '净化'];
            const bodyText = document.body.textContent;
            const foundSpells = spellNames.filter(s => bodyText.includes(s));

            // 技能加点: 找到 QWER 序列
            const skillMatch = bodyText.match(/([QWER]{15,18})/);
            const skillOrder = skillMatch ? skillMatch[1] : '';

            // 连招 — 多语言搜索 (中文 / English / 한국어)
            const combos = [];
            // 模式1: 明确的连招标签
            const comboPatterns = [
                // 中文
                /基础连招\\s*[:：]\\s*([^\\n]{5,80})/,
                /进阶连招\\s*[:：]\\s*([^\\n]{5,80})/,
                /连招\\s*[:：]\\s*([^\\n]{5,80})/,
                // English
                /basic\\s*combo\\s*[:：]\\s*([^\\n]{5,80})/i,
                /advanced\\s*combo\\s*[:：]\\s*([^\\n]{5,80})/i,
                /combo\\s*[:：]\\s*([^\\n]{5,80})/i,
                // 한국어
                /기본\\s*콤보\\s*[:：]\\s*([^\\n]{5,80})/,
                /심화\\s*콤보\\s*[:：]\\s*([^\\n]{5,80})/,
                /콤보\\s*[:：]\\s*([^\\n]{5,80})/,
                /스킬\\s*콤보\\s*[:：]\\s*([^\\n]{5,80})/,
            ];
            for (const pat of comboPatterns) {
                const m = bodyText.match(pat);
                if (m) {
                    const seq = m[1].trim();
                    if (!combos.includes(seq)) combos.push(seq);
                }
            }
            // 模式2: 在文本中寻找箭头/连字符连接的 QWER 序列 (如 Q→E→W 或 Q>E>W>Q)
            if (combos.length === 0) {
                const arrowComboRe = /[QWER](\\s*[→>\\-]\\s*[QWER]){2,9}/gi;
                const arrowMatches = bodyText.match(arrowComboRe);
                if (arrowMatches) {
                    for (const m of arrowMatches) {
                        const seq = m.replace(/\\s+/g, '').replace(/\\-/g, '→');
                        if (!combos.includes(seq)) combos.push(seq);
                    }
                }
            }

            return {
                perks: perkImgs, styles: styleImgs, items: itemImgs,
                foundSpells, skillOrder, combos,
            };
        }""")

        # 解析符文
        perk_ids = [x["id"] for x in data.get("perks", []) if x["id"]]
        style_ids = [x["id"] for x in data.get("styles", []) if x["id"]]
        if style_ids:
            result["runes"]["primary"] = style_ids[0] if len(style_ids) >= 1 else None
            result["runes"]["sub"] = style_ids[1] if len(style_ids) >= 2 else None
        # 符文: 轮换行排列 (主系4行 + 副系2行) -> 按顺序取
        if perk_ids:
            result["runes"]["perks"] = perk_ids[:6]
            result["runes"]["shards"] = perk_ids[6:9] if len(perk_ids) > 6 else []

        # 召唤师技能: 文本匹配
        spell_name_map = {
            "闪现": 4, "幽灵疾步": 14, "疾跑": 14, "治疗": 7, "治疗术": 7,
            "屏障": 21, "虚弱": 3, "点燃": 2, "引燃": 2, "传送": 1,
            "惩戒": 11, "净化": 5,
        }
        found_spells = data.get("foundSpells", [])
        spell_ids = []
        for sn in found_spells:
            sp_key = spell_name_map.get(sn)
            if sp_key and sp_key not in spell_ids:
                spell_ids.append(sp_key)
        if spell_ids:
            result["spells"] = spell_ids[:2]

        # 技能加点
        skill_order = data.get("skillOrder", "")
        if skill_order and len(skill_order) >= 15:
            result["skill_order"] = list(skill_order)
            from collections import Counter
            cnt = Counter(skill_order)
            result["skill_master"] = [k for k, v in cnt.most_common(3)]

        # 装备
        item_ids = [x["id"] for x in data.get("items", []) if x["id"]]
        real_items = [i for i in item_ids if i not in CONSUMABLES and i not in BOOT_IDS]
        if real_items:
            result["core_items"] = real_items[:3]
            result["item4"] = real_items[3:4]
            result["item5"] = real_items[4:5]

        # 连招 — 智能去重与分类
        combos = data.get("combos", [])
        seen = set()
        for seq in combos:
            key = seq.replace("→", ">").replace("-", ">").replace(" ", "").upper()
            if key not in seen:
                seen.add(key)
                # 简单启发: 包含 > 或 3+ 技能步骤 → advanced
                has_arrow = any(c in seq for c in "→>-")
                steps = len([c for c in seq.upper() if c in "QWER"])
                ctype = "medium" if (has_arrow and steps >= 3) else "basic"
                result["combos"].append({"type": ctype, "sequence": seq})
        # 最多保留 4 条, 优先保留 advanced
        result["combos"].sort(key=lambda c: (0 if c["type"] == "basic" else 1))
        result["combos"] = result["combos"][:4]

    except Exception as e:
        print(f"    [build DOM extract error] {e}")

    return result, None


# ======================== OP.GG Tier 抓取 ========================
def scrape_tier_page(page, role, region, tier, champions_by_name=None):
    """抓取分路排名。返回 list[{key, rank, win, pick, games}]"""
    params = f"position={role}&region={region}&tier={tier}"
    url = TIER_URL + "?" + params
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(POST_LOAD_MS)
    except Exception as e:
        return None, f"page load error: {e}"

    if champions_by_name is None:
        champions_by_name = {}

    try:
        data = page.evaluate("""() => {
            // 找包含 champion 排名数据的表格或列表
            const main = document.querySelector('main');
            if (!main) return [];

            // 方法: 找所有 <tr> 或 <li> 包含胜率数据的
            const items = [];
            const rows = [...main.querySelectorAll('tr, li')];

            for (const row of rows) {
                const img = row.querySelector('img[alt]');
                const text = row.textContent.trim();
                const pctMatch = text.match(/(\\d+\\.?\\d*)%/);
                const numMatch = text.match(/(\\d{3,7})\\s*$/);
                if (img && img.alt.length > 1 && img.alt.length < 20 && pctMatch && numMatch) {
                    items.push({
                        name: img.alt,
                        winRate: parseFloat(pctMatch[1]),
                        games: parseInt(numMatch[1]),
                    });
                }
            }
            return items;
        }""")

        if data:
            rows = []
            for i, item in enumerate(data):
                opp_key = champions_by_name.get(item.get("name", ""))
                if opp_key:
                    rows.append({
                        "key": opp_key,
                        "rank": i + 1,
                        "win": float(item["winRate"]),
                        "pick": None,
                        "games": int(item["games"]),
                    })
            if rows:
                return rows, None
    except Exception as e:
        print(f"    [tier DOM extract error] {e}")

    return None, "no data found"


# ======================== 分路名单解析 ========================
def load_lane_pool(champs):
    """解析 lanes.txt, 返回 {role: set(slug), ...} 和 {slug: [role, ...]}。
    用于预过滤——只抓英雄真正打的分路。"""
    lanes_path = os.path.join(HERE, "lanes.txt")
    if not os.path.exists(lanes_path):
        return None, None

    # 建立中文名/称号 → slug 映射
    name_to_slug = {}
    for c in champs:
        if c["name"]:
            name_to_slug[c["name"]] = c["slug"]
        if c["title"]:
            name_to_slug[c["title"]] = c["slug"]

    role_headers = {"上路": "top", "打野": "jungle", "中路": "mid", "下路": "adc", "辅助": "support"}
    pool = {r: set() for r in role_headers.values()}
    cur_role = None

    with open(lanes_path, encoding="utf-8") as f:
        text = f.read()
    text = text.lstrip("﻿")  # BOM

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        hdr = line.rstrip("：:").strip()
        if hdr in role_headers:
            cur_role = role_headers[hdr]
            continue
        if cur_role is None:
            continue
        if any(bad in line for bad in ("对比", "图片", "去重", "出现", "总结", "我来", "其中")):
            continue

        # 按序号拆分成多个英雄名
        parts = re.split(r'\d+\s*[\.\)、]\s*', line)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 提取括号内外的名称
            mb = re.search(r"[（(]([^）)]+)[）)]", part)
            names = []
            if mb:
                names.append(mb.group(1))
                names.append(re.sub(r"[（(][^）)]*[）)]", "", part))
            else:
                names.append(part)
            for nm in names:
                slug = name_to_slug.get(nm.strip())
                if slug:
                    pool[cur_role].add(slug)
                    break

    # 构建反向映射
    slug_to_roles = {}
    for role, slugs in pool.items():
        for slug in slugs:
            slug_to_roles.setdefault(slug, []).append(role)

    print(f"[*] lanes.txt: {sum(len(v) for v in pool.values())} 条英雄-分路映射")
    return pool, slug_to_roles


# ======================== 进度管理 ========================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ======================== 主流程 ========================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", metavar="SLUG", help="只抓该英雄 counter+build 并 dump 调试数据")
    ap.add_argument("--limit", type=int, help="每分路只抓前 N 个英雄")
    ap.add_argument("--fresh", action="store_true", help="忽略已有进度, 重新抓")
    ap.add_argument("--only", help="只抓某类: counter,build,tier (逗号分隔)")
    ap.add_argument("--roles", help="只抓这些分路, 逗号分隔")
    ap.add_argument("--fast", action="store_true", help="快速模式: 仅 Diamond+Master, 仅 Global+CN, 严格按 lanes.txt 分路")
    ap.add_argument("--full", action="store_true", help="全量模式: 所有地区+所有段位+所有英雄组合")
    args = ap.parse_args()

    champs = fetch_champions()
    save_json(OUT_CHAMPS, champs)
    print(f"[+] 英雄映射 -> {OUT_CHAMPS}")
    by_key = {c["key"]: c for c in champs}

    # 构建中文名 → key 映射
    name_to_key = {}
    for c in champs:
        if c["name"]:
            name_to_key[c["name"]] = c["key"]
        if c["title"]:
            name_to_key[c["title"]] = c["key"]

    # ---- debug ----
    if args.debug:
        slug = args.debug.lower()
        # 从 lanes.txt 找到该英雄的主要分路
        debug_role = "support"
        try:
            lane_pool, slug_to_roles = load_lane_pool(champs)
            roles = slug_to_roles.get(slug, [])
            if roles:
                debug_role = roles[0]
        except Exception:
            pass
        print(f"[*] DEBUG: {slug} (角色={debug_role})")
        browser = launch_browser()
        page, ctx = new_page()
        try:
            print(f"[*] 抓 counter ({debug_role}/global/diamond)...")
            rows, err = scrape_counter_page(page, slug, debug_role, "global", "diamond", name_to_key)
            if err:
                print(f"[!] counter 失败: {err}")
            else:
                print(f"[+] counter: {len(rows)} 条")
                for x in rows[:10]:
                    opp = by_key.get(x["opp"], {}).get("name", x["opp"])
                    print(f"    vs {opp}: 胜率 {x['win']}%  ({x['games']} 局)")
                save_json(os.path.join(HERE, f"debug_{slug}_counter.json"), rows)

            print(f"[*] 抓 build ({debug_role})...")
            build, err = scrape_build_page(page, slug, debug_role)
            if err:
                print(f"[!] build 失败: {err}")
            else:
                print(f"[+] build: {json.dumps(build, ensure_ascii=False)[:400]}")
                save_json(os.path.join(HERE, f"debug_{slug}_build.json"), build)

            print(f"[*] 抓 tier ({debug_role}/global/diamond)...")
            tier_rows, err = scrape_tier_page(page, debug_role, "global", "diamond", name_to_key)
            if err:
                print(f"[!] tier 失败: {err}")
            else:
                print(f"[+] tier: {len(tier_rows)} 项")
                for x in tier_rows[:8]:
                    nm = by_key.get(x["key"], {}).get("name", x["key"])
                    print(f"    #{x['rank']} {nm}: 胜率 {x['win']}")
                save_json(os.path.join(HERE, f"debug_{slug}_tier.json"), tier_rows)
        finally:
            ctx.close()
            close_browser()
        return

    roles = list(ROLES.values())
    if args.roles:
        roles = [ROLES.get(r.strip(), r.strip()) for r in args.roles.split(",")]

    only = set(s.strip() for s in args.only.split(",")) if args.only else {"counter", "build", "tier"}
    progress = {} if args.fresh else load_json(PROGRESS, {})

    # ---- 剪枝策略 ----
    lane_pool, slug_to_roles = load_lane_pool(champs)

    if args.fast:
        # 快速模式: 仅 Diamond+Master, 仅 Global+CN, 严格按 lanes.txt 分路
        active_regions = {"global": "global", "cn": "cn"}
        active_tiers = {"diamond": "diamond", "master": "master"}
        use_lane_filter = True
        print("[*] 快速模式: Diamond+Master, Global+CN, 仅正常分路")
    elif args.full:
        # 全量模式: 所有组合
        active_regions = dict(REGIONS)
        active_tiers = dict(TIERS)
        use_lane_filter = False
        print("[*] 全量模式: 所有地区+段位+英雄")
    else:
        # 默认模式: 智能剪枝
        active_regions = {"global": "global", "cn": "cn"}
        active_tiers = {"diamond": "diamond", "master": "master"}
        use_lane_filter = True
        # 韩国服单独可选
        if args.roles is None:  # 用户没指定特定分路时才抓 KR
            pass  # KR 默认不抓, 需要的话加 --full
        print(f"[*] 默认模式: Diamond+Master, Global+CN, 仅正常分路 (用 --full 全量抓取)")

    # 构建每分路的英雄列表
    role_heroes = {}
    for role in roles:
        if use_lane_filter and lane_pool:
            # 只抓 lanes.txt 中该分路的英雄
            role_slugs = lane_pool.get(role, set())
            role_heroes[role] = [c for c in champs if c["slug"] in role_slugs]
            print(f"  {role}: {len(role_heroes[role])} 个英雄 (已过滤)")
        else:
            role_heroes[role] = champs[:args.limit] if args.limit else champs
            print(f"  {role}: {len(role_heroes[role])} 个英雄 (全部)")

    if args.limit:
        for role in roles:
            role_heroes[role] = role_heroes[role][:args.limit]

    # Meta
    if {"build"} & only:
        try:
            meta = fetch_meta()
            save_json(OUT_META, meta)
            print(f"[+] 元数据 -> {OUT_META}")
        except Exception as e:
            print(f"[!] 元数据下载失败: {e}")

    browser = launch_browser()

    try:
        # ---- COUNTER ----
        if "counter" in only:
            print("\n========== 抓取 克制(counter) ==========")
            data = {} if args.fresh else load_json(OUT_COUNTER, {})
            total_pages = 0
            total_ok = 0
            page, ctx = new_page()  # 复用同一个页面, 避免每次都触发 Cloudflare 挑战
            try:
                for role in roles:
                    data.setdefault(role, {})
                    heroes = role_heroes.get(role, [])
                    for c in heroes:
                        slug = c["slug"]
                        done_key = f"counter/{role}/{slug}"
                        if progress.get(done_key) == "done":
                            continue
                        data[role].setdefault(slug, {})
                        for region in active_regions.values():
                            data[role][slug].setdefault(region, {})
                            for tier in active_tiers.values():
                                total_pages += 1
                                try:
                                    rows, err = scrape_counter_page(page, slug, role, region, tier, name_to_key)
                                    tag = f"counter/{role}/{slug}/{region}/{tier}"
                                    if err:
                                        if total_pages <= 20 or "insufficient" not in err:
                                            print(f"  - {tag}: {err}")
                                    else:
                                        data[role][slug][region][tier] = rows
                                        total_ok += 1
                                        print(f"  + {tag}: {len(rows)} 条")
                                except Exception as e:
                                    print(f"  ! {tag}: {e}")
                                time.sleep(0.8)
                        progress[done_key] = "done"
                        save_json(OUT_COUNTER, data)
                        save_json(PROGRESS, progress)
            finally:
                ctx.close()
            save_json(OUT_COUNTER, data)
            print(f"[+] counter -> {OUT_COUNTER} (共 {total_ok}/{total_pages} 页成功)")

        # ---- BUILD ----
        if "build" in only:
            print("\n========== 抓取 出装(build) ==========")
            bdata = {} if args.fresh else load_json(OUT_BUILD, {})
            page, ctx = new_page()  # 复用同一个页面
            try:
                for role in roles:
                    bdata.setdefault(role, {})
                    heroes = role_heroes.get(role, [])
                    for c in heroes:
                        slug = c["slug"]
                        done_key = f"build/{role}/{slug}"
                        if progress.get(done_key) == "done":
                            continue
                        try:
                            b, err = scrape_build_page(page, slug, role)
                            tag = f"build/{role}/{slug}"
                            if err:
                                print(f"  - {tag}: {err}")
                            else:
                                bdata[role][slug] = b
                                rp = b['runes'].get('perks', [])
                                ci = b['core_items']
                                co = b['combos']
                                print(f"  + {tag}: 符文{len(rp)} 装{len(ci)} 连招{len(co)}")
                        except Exception as e:
                            print(f"  ! {tag}: {e}")
                        progress[done_key] = "done"
                        save_json(OUT_BUILD, bdata)
                        save_json(PROGRESS, progress)
                        time.sleep(0.8)
            finally:
                ctx.close()
            save_json(OUT_BUILD, bdata)
            print(f"[+] build -> {OUT_BUILD}")

        # ---- TIER ----
        if "tier" in only:
            print("\n========== 抓取 排名(tier) ==========")
            tdata = {} if args.fresh else load_json(OUT_TIER, {})
            page, ctx = new_page()  # 复用同一个页面
            try:
                for role in roles:
                    tdata.setdefault(role, {})
                    for region in active_regions.values():
                        tdata[role].setdefault(region, {})
                        for tier in active_tiers.values():
                            done_key = f"tier/{role}/{region}/{tier}"
                            if progress.get(done_key) == "done":
                                continue
                            try:
                                rows, err = scrape_tier_page(page, role, region, tier, name_to_key)
                                tag = f"tier/{role}/{region}/{tier}"
                                if err:
                                    print(f"  - {tag}: {err}")
                                else:
                                    tdata[role][region][tier] = rows
                                    print(f"  + {tag}: {len(rows)} 项")
                            except Exception as e:
                                print(f"  ! {tag}: {e}")
                            progress[done_key] = "done"
                            save_json(OUT_TIER, tdata)
                            save_json(PROGRESS, progress)
                            time.sleep(0.8)
            finally:
                ctx.close()
            save_json(OUT_TIER, tdata)
            print(f"[+] tier -> {OUT_TIER}")

    finally:
        close_browser()

    print(f"\n[+] 全部完成!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[中断] 进度已保存, 再次运行可续抓。")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
