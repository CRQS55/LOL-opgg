#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py — 使用 Playwright (真实浏览器) 抓取 op.gg 克制/出装/排名数据。

用法:
    python scrape.py                      # 正常抓取(断点续抓)
    python scrape.py --fresh              # 忽略进度重新抓
    python scrape.py --debug thresh       # 只抓 thresh 并 dump 调试数据
    python scrape.py --limit 3            # 每分路只抓前 3 个英雄(快速验证)
    python scrape.py --heroes ahri,zed    # 只抓指定英雄
    python scrape.py --only counter       # 只抓 counter
    python scrape.py --only build,tier    # 只抓 build 和 tier
    python scrape.py --concurrency 12     # 并发标签页数(默认 8)
    python scrape.py --fast               # 快速模式: Diamond+Master, Global+CN
    python scrape.py --full               # 全量模式: 所有地区+段位

依赖:
    pip install playwright requests
    playwright install chromium

v2 调优要点(相对旧版):
    1. 单浏览器 + 多标签页并发(默认 8 个), 网络等待与解析重叠执行。
    2. 拦截图片/字体/样式表/第三方统计与广告请求, 单页首屏从 ~7s 降到 ~2.4s。
    3. 用 wait_for_function 等待"真实数据节点"(百分比 strong / 符文图标 / 排名表行),
       取代固定 POST_LOAD_MS=5000 死等; 数据一到就解析。
    4. 逐页重试 + 指数退避 + 抖动; 识别 CloudFront 403 / Cloudflare 挑战页并单独退避。
    5. 失败页绝不写入进度; 本轮结束自动再补抓; 下次运行仍会重抓未成功的页。
    6. 原子写盘(临时文件 + os.replace) + 节流保存, 中途崩溃不再损坏 JSON。
    7. 排名(tier)解析改为按表格行读取, 修复旧版 "no data found"。
"""

import argparse
import asyncio
import glob as _glob
import json
import os
import random
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))

REGIONS = {"global": "global", "kr": "kr", "cn": "cn"}
TIERS = {"diamond": "diamond", "master": "master", "grandmaster": "grandmaster", "challenger": "challenger"}
ROLES = {"top": "top", "jungle": "jungle", "mid": "mid", "adc": "adc", "support": "support"}
ROLE_ORDER = ["top", "jungle", "mid", "adc", "support"]

DEFAULT_BASE = "https://op.gg"
LOCALE = "zh-cn"

# op.gg 数据(zh-cn 路由)直连请求即可 200, 无需额外地区前缀。
COUNTER_PATH = "/{locale}/lol/champions/{slug}/counters/{role}?region={region}&tier={tier}"
BUILD_PATH = "/{locale}/lol/champions/{slug}/build/{role}"
TIER_PATH = "/{locale}/lol/champions?position={role}&region={region}&tier={tier}"

DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/champion.json"
DDRAGON_ITEMS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/item.json"
DDRAGON_SUMMS = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/summoner.json"
DDRAGON_RUNES = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/runesReforged.json"
DDRAGON_IMG = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/"
RUNE_IMG = "https://ddragon.leagueoflegends.com/cdn/img/"

# 属性碎片(StatMods: 适应之力/攻速/技能急速...)不在 Data Dragon 的 runesReforged.json 里,
# 需要单独取一份, 否则前端只能显示 "符文5005" 且没有图标。
SHARD_URL = ("https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
             "global/zh_cn/v1/perks.json")
SHARD_ID_MIN, SHARD_ID_MAX = 5001, 5013
# 网络不可用时的内置表(依据 CommunityDragon zh_cn perks.json, 2026-11 实测):
#   5001=成长生命值  5011=生命值  5005=攻击速度  5007=技能急速
#   5008=适应之力    5010=移动速度 5013=韧性和减速抗性
SHARD_FALLBACK = {
    "5001": ("成长生命值", "StatModsHealthPlusIcon.png"),
    "5002": ("护甲", "StatModsArmorIcon.png"),
    "5003": ("魔法抗性", "StatModsMagicResIcon.png"),
    "5005": ("攻击速度", "StatModsAttackSpeedIcon.png"),
    "5007": ("技能急速", "StatModsCDRScalingIcon.png"),
    "5008": ("适应之力", "StatModsAdaptiveForceIcon.png"),
    "5010": ("移动速度", "StatModsMovementSpeedIcon.png"),
    "5011": ("生命值", "StatModsHealthScalingIcon.png"),
    "5012": ("抗性成长", "StatModsAdaptiveForceScalingIcon.png"),
    "5013": ("韧性和减速抗性", "StatModsTenacityIcon.png"),
}

# 这些路径会在 --data-dir 改变时重新指向
OUT_COUNTER = os.path.join(HERE, "counter_data.json")
OUT_CHAMPS = os.path.join(HERE, "champions.json")
OUT_BUILD = os.path.join(HERE, "build_data.json")
OUT_TIER = os.path.join(HERE, "tier_data.json")
OUT_META = os.path.join(HERE, "meta.json")
PROGRESS = os.path.join(HERE, ".scrape_progress.json")


def set_data_dir(path):
    """把输出文件重定向到指定目录(默认就是脚本目录)。"""
    global OUT_COUNTER, OUT_CHAMPS, OUT_BUILD, OUT_TIER, OUT_META, PROGRESS
    os.makedirs(path, exist_ok=True)
    OUT_COUNTER = os.path.join(path, "counter_data.json")
    OUT_CHAMPS = os.path.join(path, "champions.json")
    OUT_BUILD = os.path.join(path, "build_data.json")
    OUT_TIER = os.path.join(path, "tier_data.json")
    OUT_META = os.path.join(path, "meta.json")
    PROGRESS = os.path.join(path, ".scrape_progress.json")


# ======================== 可调参数 ========================
DEFAULT_CONCURRENCY = 8
DEFAULT_PAGE_TIMEOUT = 20      # 单页超时(秒)
DEFAULT_RETRIES = 3            # 单页尝试次数
DEFAULT_MIN_DELAY = 0.15       # 每个页面请求之间的最小间隔(秒)
SAVE_INTERVAL = 5.0            # 数据落盘节流(秒)
PROGRESS_INTERVAL = 2.0        # 进度落盘节流(秒)

_VER = {"v": None}
BOOT_IDS = {3006, 3009, 3020, 3047, 3111, 3117, 3158}
CONSUMABLES = {2003, 2004, 2031, 2032, 2033, 2055, 2138, 2139, 2140, 3340, 3341, 3363, 3364, 3599}

# op.gg 的"数据不足"文案有多种写法(旧版只匹配了其中一种)
NODATA_MARKERS = ("数据样本数不足", "数据样本不足", "样本数据不足", "数据不足")
# 被拦截/挑战页特征
BLOCK_MARKERS = (
    "403 ERROR", "Request blocked", "Just a moment", "Attention Required",
    "verify you are human", "Verify you are human", "cf-browser-verification",
    "请稍候", "检查您的浏览器", "Enable JavaScript and cookies",
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 只影响渲染, 对 DOM 文本抓取无用的资源: 直接中止, 单页省 3-5s
BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet", "imageset"}
BLOCK_HOSTS = (
    "googletagmanager.com", "google-analytics.com", "googlesyndication.com",
    "doubleclick.net", "googleadservices.com", "google.com/ccm", "rybbit.com",
    "zendesk.com", "inmobi.com", "cmp.inmobi.com", "our.gg", "s-opgg-kit.op.gg",
    "fonts.googleapis.com", "fonts.gstatic.com", "facebook.net", "criteo",
    "taboola", "outbrain", "adsystem", "adnxs.com", "scorecardresearch",
)


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


def parse_shard_meta(payload, icon_base=RUNE_IMG):
    """CommunityDragon perks.json -> {id: {name, icon}}。

    payload 为空/解析失败时返回内置表, 保证 meta.json 里一定有这些碎片。
    """
    out = {}
    for p in payload or []:
        if not isinstance(p, dict):
            continue
        try:
            pid = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if not (SHARD_ID_MIN <= pid <= SHARD_ID_MAX):
            continue
        name = (p.get("name") or "").strip()
        icon_file = (p.get("iconPath") or "").rsplit("/", 1)[-1]
        if name and icon_file.endswith(".png"):
            out[str(pid)] = {"name": name,
                             "icon": icon_base + "perk-images/StatMods/" + icon_file}
    for pid, (name, fn) in SHARD_FALLBACK.items():
        out.setdefault(pid, {"name": name,
                             "icon": icon_base + "perk-images/StatMods/" + fn})
    return out


def fetch_shards():
    """取属性碎片名称+图标(失败用内置表兜底)。"""
    import requests as req
    payload = None
    try:
        payload = req.get(SHARD_URL, timeout=20).json()
    except Exception as e:
        print(f"    [!] 属性碎片元数据下载失败({e.__class__.__name__}), 用内置映射兜底")
    return parse_shard_meta(payload, RUNE_IMG)


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
    # 属性碎片(5001/5005/5007/5008/5010/5011/5013...)不在 runesReforged.json 里,
    # 单独从 CommunityDragon 取; 取不到就用内置表兜底, 否则前端只显示 "符文5005" 且没图标。
    shards = fetch_shards()
    meta["runes"].update(shards)
    print(f"    物品 {len(items)} · 召唤师技能 {len(summs)} · 符文 {len(meta['runes'])}"
          f"(含属性碎片 {len(shards)})")
    return meta


# ======================== URL 构造(纯函数, 便于穷举验证) ========================
def url_counter(base, slug, role, region, tier):
    return base.rstrip("/") + COUNTER_PATH.format(locale=LOCALE, slug=slug, role=role,
                                                  region=region, tier=tier)


def url_build(base, slug, role):
    return base.rstrip("/") + BUILD_PATH.format(locale=LOCALE, slug=slug, role=role)


def url_tier(base, role, region, tier):
    return base.rstrip("/") + TIER_PATH.format(locale=LOCALE, role=role, region=region, tier=tier)


# ======================== 判定函数(纯函数, 便于穷举验证) ========================
def is_nodata_text(text):
    """页面是否显示"样本不足"。"""
    return any(m in text for m in NODATA_MARKERS)


def is_blocked_text(text, title=""):
    """是否为 CloudFront 403 / Cloudflare 挑战 / JS 校验页。"""
    blob = (title or "") + "\n" + (text or "")
    return any(m in blob for m in BLOCK_MARKERS)


def backoff_delay(attempt, rng=None, base=0.8, cap=8.0, blocked=False):
    """指数退避 + 抖动。attempt 从 1 开始。返回值永不超过 cap。"""
    r = rng or random
    d = base * (2 ** max(0, attempt - 1))
    if blocked:
        d = max(d, 2.5) * 1.6
    d = min(cap, d)
    jitter = r.uniform(0, min(0.6, d * 0.35))
    return round(min(cap, d + jitter), 3)


# ======================== 页面内的等待/解析 JS ========================
# 快速探针: 拿到标题 + 首屏文本, 用来抢在 selector 超时前识别拦截页
PROBE_JS = r"""() => {
    const body = document.body;
    return {
        title: document.title || '',
        head: body ? (body.innerText || '').slice(0, 600) : '',
        hasMain: !!document.querySelector('main'),
    };
}"""

WAIT_COUNTER_JS = r"""() => {
    const main = document.querySelector('main');
    if (!main) return 'nom';
    const strongs = [...main.querySelectorAll('strong')]
        .filter(s => /^\d+(\.\d+)?%$/.test((s.textContent || '').trim()));
    if (strongs.length) return 'data';
    const t = document.body ? document.body.innerText || '' : '';
    if (t.includes('数据样本数不足') || t.includes('数据样本不足') ||
        t.includes('样本数据不足') || t.includes('数据不足')) return 'nodata';
    return false;
}"""

WAIT_BUILD_JS = r"""() => {
    const body = document.body;
    if (!body) return false;
    if (document.querySelector("img[src*='/perk/']") ||
        document.querySelector("img[src*='/item/']")) return 'data';
    const t = body.innerText || '';
    if (t.includes('数据样本数不足') || t.includes('样本数据不足') || t.includes('数据不足')) return 'nodata';
    return false;
}"""

WAIT_TIER_JS = r"""() => {
    const main = document.querySelector('main');
    if (!main) return 'nom';
    if (main.querySelector("table tbody tr a[href*='/champions/']")) return 'data';
    const t = document.body ? document.body.innerText || '' : '';
    if (t.includes('数据不足') || t.includes('暂无')) return 'nodata';
    return false;
}"""

EXTRACT_COUNTER_JS = r"""() => {
    const main = document.querySelector('main');
    if (!main) return {rows: [], hasMain: false};
    const aside = main.querySelector('aside');
    const container = aside || main;

    const items = [];
    const strongEls = [...container.querySelectorAll('strong')]
        .filter(s => /^\d+(\.\d+)?%$/.test((s.textContent || '').trim()));

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

        const fullText = (li.textContent || '').replace(/\s+/g, ' ').trim();
        const pct = parseFloat((strong.textContent || '').trim());
        const numMatch = fullText.match(/(\d{2,7})\s*$/);
        const games = numMatch ? parseInt(numMatch[1], 10) : 0;
        if (!seen.has(img.alt) && games > 0) {
            seen.add(img.alt);
            items.push({name: img.alt, winRate: pct, games: games});
        }
    }

    // 备用: 直接按 li 文本匹配
    if (!items.length) {
        for (const li of container.querySelectorAll('li')) {
            const img = li.querySelector('img[alt]');
            if (!img || !img.alt || img.alt.length < 2 || img.alt.length > 20) continue;
            const text = (li.textContent || '').trim();
            const pctMatch = text.match(/(\d+(?:\.\d+)?)%/);
            const numMatch = text.match(/(\d{2,7})\s*$/);
            if (pctMatch && numMatch) {
                items.push({name: img.alt, winRate: parseFloat(pctMatch[1]),
                            games: parseInt(numMatch[1], 10)});
            }
        }
    }

    const t = document.body ? document.body.innerText || '' : '';
    return {
        rows: items,
        hasMain: true,
        hasAside: !!aside,
        nodata: !items.length && (t.includes('数据样本数不足') || t.includes('数据样本不足') ||
                                  t.includes('样本数据不足') || t.includes('数据不足')),
        textLen: t.length,
    };
}"""

EXTRACT_BUILD_JS = r"""() => {
    const allImgs = [...document.querySelectorAll('img')];
    function getNum(src) {
        const parts = src.split('?')[0].split('/').pop().split('.')[0];
        const m = parts.match(/^(\d{2,5})$/);
        return m ? parseInt(m[1], 10) : null;
    }
    function isSelectedPerk(img) {
        const cls = (img.getAttribute('class') || '') + ' ' +
            ((img.parentElement && img.parentElement.getAttribute
                ? img.parentElement.getAttribute('class') || '' : '') + ' ' +
             (img.parentElement && img.parentElement.parentElement &&
              img.parentElement.parentElement.getAttribute
                ? img.parentElement.parentElement.getAttribute('class') || '' : ''));
        if (/opacity-50|grayscale/.test(cls)) return false;
        return true;
    }
    function getRuneData() {
        const styleImgsWithIdx = [];
        const perkImgsWithIdx = [];
        for (let i = 0; i < allImgs.length; i++) {
            const src = allImgs[i].src;
            if (/\/perkStyle\//.test(src)) {
                const id = getNum(src);
                if (id) styleImgsWithIdx.push({idx: i, id, alt: allImgs[i].alt});
            }
            if ((/\/perk\/\d+\.png/.test(src) || /\/perkShard\//.test(src)) && !/\/perkStyle\//.test(src)) {
                if (isSelectedPerk(allImgs[i])) {
                    const id = getNum(src);
                    if (id) perkImgsWithIdx.push({idx: i, id, alt: allImgs[i].alt});
                }
            }
        }
        if (styleImgsWithIdx.length < 2) return null;
        const page1Styles = styleImgsWithIdx.slice(0, 2);
        const boundary = styleImgsWithIdx.length >= 3 ? styleImgsWithIdx[2].idx : 999999;
        const page1Perks = perkImgsWithIdx.filter(p => p.idx < boundary);
        const seen = new Set();
        const perks = page1Perks.filter(p => { if (seen.has(p.id)) return false; seen.add(p.id); return true; });
        return {perks, styles: page1Styles};
    }
    const runeData = getRuneData();
    const perkImgs = runeData ? runeData.perks
        : allImgs.filter(img => /\/perk\/\d+\.png/.test(img.src)).map(img => ({alt: img.alt, id: getNum(img.src)}));
    const styleImgs = runeData ? runeData.styles
        : allImgs.filter(img => /\/perkStyle\//.test(img.src)).map(img => ({alt: img.alt, id: getNum(img.src)}));
    const itemImgs = allImgs.filter(img => /\/item\/\d+\.png/.test(img.src))
        .map(img => ({alt: img.alt, id: getNum(img.src)}));

    const spellNames = ['闪现', '点燃', '传送', '惩戒', '疾跑', '治疗', '屏障', '虚弱', '净化'];
    const bodyText = document.body ? (document.body.textContent || '') : '';
    const foundSpells = spellNames.filter(s => bodyText.includes(s));

    const skillMatch = bodyText.match(/([QWER]{15,18})/);
    const skillOrder = skillMatch ? skillMatch[1] : '';

    const combos = [];
    const comboPatterns = [
        /基础连招\s*[:：]\s*([^\n]{5,80})/, /进阶连招\s*[:：]\s*([^\n]{5,80})/,
        /连招\s*[:：]\s*([^\n]{5,80})/,
        /basic\s*combo\s*[:：]\s*([^\n]{5,80})/i, /advanced\s*combo\s*[:：]\s*([^\n]{5,80})/i,
        /combo\s*[:：]\s*([^\n]{5,80})/i,
        /기본\s*콤보\s*[:：]\s*([^\n]{5,80})/, /심화\s*콤보\s*[:：]\s*([^\n]{5,80})/,
        /콤보\s*[:：]\s*([^\n]{5,80})/, /스킬\s*콤보\s*[:：]\s*([^\n]{5,80})/,
    ];
    for (const pat of comboPatterns) {
        const m = bodyText.match(pat);
        if (m) {
            const seq = m[1].trim();
            if (!combos.includes(seq)) combos.push(seq);
        }
    }
    if (combos.length === 0) {
        const arrowComboRe = /[QWER](\s*[→>\-]\s*[QWER]){2,9}/gi;
        const arrowMatches = bodyText.match(arrowComboRe);
        if (arrowMatches) {
            for (const m of arrowMatches) {
                const seq = m.replace(/\s+/g, '').replace(/\-/g, '→');
                if (!combos.includes(seq)) combos.push(seq);
            }
        }
    }

    return {perks: perkImgs, styles: styleImgs, items: itemImgs,
            foundSpells: foundSpells, skillOrder: skillOrder, combos: combos};
}"""

EXTRACT_TIER_JS = r"""() => {
    const main = document.querySelector('main');
    if (!main) return {rows: [], hasMain: false};
    const rows = [];
    let rank = 0;
    for (const tr of main.querySelectorAll('tr')) {
        const a = tr.querySelector("a[href*='/champions/']");
        if (!a) continue;
        const href = a.getAttribute('href') || '';
        const m = href.match(/\/champions\/([A-Za-z0-9]+)\/build(?:\/([a-z]+))?/);
        if (!m) continue;
        const strong = tr.querySelector('strong');
        const img = tr.querySelector('img');
        const name = ((strong && strong.textContent) || (img && img.alt) || '').trim();
        const pcts = [...tr.querySelectorAll('td')]
            .map(td => (td.textContent || '').trim())
            .filter(t => /^\d+(\.\d+)?%$/.test(t))
            .map(t => parseFloat(t));
        if (!pcts.length) continue;
        rank += 1;
        rows.push({
            slug: m[1].toLowerCase(),
            name: name,
            rank: rank,
            win: pcts[0],
            pick: pcts.length > 1 ? pcts[1] : null,
            ban: pcts.length > 2 ? pcts[2] : null,
        });
    }
    const t = document.body ? document.body.innerText || '' : '';
    return {rows: rows, hasMain: true,
            nodata: !rows.length && (t.includes('数据不足') || t.includes('暂无'))};
}"""


# ======================== 解析(纯函数) ========================
def finalize_counter_rows(raw_rows, name_to_key):
    """把页面里抓到的 {name, winRate, games} 映射成 {opp: key, win, games}。"""
    rows = []
    seen = set()
    for item in raw_rows or []:
        key = name_to_key.get((item.get("name") or "").strip())
        if not key or key in seen:
            continue
        try:
            win = float(item.get("winRate"))
        except (TypeError, ValueError):
            continue
        games = int(item.get("games") or 0)
        if games <= 0:
            continue
        seen.add(key)
        rows.append({"opp": key, "win": win, "games": games})
    return rows


def finalize_tier_rows(raw_rows, champ_by_slug, name_to_key):
    """排名表格行 -> {key, rank, win, pick, games}。name 来自 <strong>, slug 来自 href。"""
    rows = []
    seen = set()
    for item in raw_rows or []:
        slug = (item.get("slug") or "").lower()
        c = champ_by_slug.get(slug)
        if not c:
            key = name_to_key.get((item.get("name") or "").strip())
            if not key:
                continue
            slug = None
        else:
            key = c["key"]
        if key in seen:
            continue
        win = item.get("win")
        if win is None:
            continue
        pick = item.get("pick")
        seen.add(key)
        rows.append({
            "key": key,
            "rank": int(item.get("rank") or len(rows) + 1),
            "win": float(win),
            "pick": float(pick) if pick is not None else None,
            "ban": float(item["ban"]) if item.get("ban") is not None else None,
            "games": None,
        })
    rows.sort(key=lambda r: r["rank"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


SPELL_NAME_MAP = {
    "闪现": 4, "幽灵疾步": 14, "疾跑": 14, "治疗": 7, "治疗术": 7,
    "屏障": 21, "虚弱": 3, "点燃": 2, "引燃": 2, "传送": 1,
    "惩戒": 11, "净化": 5,
}


def parse_build_payload(data):
    """把 build 页 JS 抓到的原始数据解析成 build_data.json 里的结构。"""
    result = {"runes": {}, "spells": [], "skill_order": [], "skill_master": [],
              "core_items": [], "item4": [], "item5": [], "combos": []}
    if not data:
        return result

    perk_ids = [x.get("id") for x in data.get("perks", []) if x.get("id")]
    style_ids = [x.get("id") for x in data.get("styles", []) if x.get("id")]
    if style_ids:
        result["runes"]["primary"] = style_ids[0]
        result["runes"]["sub"] = style_ids[1] if len(style_ids) >= 2 else None
    if perk_ids:
        result["runes"]["perks"] = perk_ids[:6]
        result["runes"]["shards"] = perk_ids[6:9] if len(perk_ids) > 6 else []

    spell_ids = []
    for sn in data.get("foundSpells", []) or []:
        sp_key = SPELL_NAME_MAP.get(sn)
        if sp_key and sp_key not in spell_ids:
            spell_ids.append(sp_key)
    if spell_ids:
        result["spells"] = spell_ids[:2]

    skill_order = data.get("skillOrder", "") or ""
    if len(skill_order) >= 15:
        result["skill_order"] = list(skill_order)
        from collections import Counter
        cnt = Counter(skill_order)
        result["skill_master"] = [k for k, v in cnt.most_common(3)]

    item_ids = [x.get("id") for x in data.get("items", []) if x.get("id")]
    real_items = [i for i in item_ids if i not in CONSUMABLES and i not in BOOT_IDS]
    if real_items:
        result["core_items"] = real_items[:3]
        result["item4"] = real_items[3:4]
        result["item5"] = real_items[4:5]

    seen = set()
    for seq in data.get("combos", []) or []:
        # 归一化: 全角箭头/->/-/空格 统一成 '>', 这样同一条连招不会重复
        key = re.sub(r"[\s\-→>]+", ">", seq).upper()
        if key in seen:
            continue
        seen.add(key)
        has_arrow = any(c in seq for c in "→>-")
        steps = len([c for c in seq.upper() if c in "QWER"])
        ctype = "medium" if (has_arrow and steps >= 3) else "basic"
        result["combos"].append({"type": ctype, "sequence": seq})
    result["combos"].sort(key=lambda c: (0 if c["type"] == "basic" else 1))
    result["combos"] = result["combos"][:4]
    return result


# ======================== 落盘 ========================
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json_atomic(path, data):
    """先写临时文件再替换, 崩溃时不会留下半截 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class JsonStore:
    """带节流的原子写入器。"""

    def __init__(self, path, data, interval=SAVE_INTERVAL):
        self.path = path
        self.data = data
        self.interval = interval
        self._last = 0.0
        self._dirty = False
        self.saves = 0

    def touch(self):
        self._dirty = True

    def maybe_save(self, force=False):
        now = time.time()
        if not self._dirty:
            return False
        if not force and (now - self._last) < self.interval:
            return False
        save_json_atomic(self.path, self.data)
        self._last = now
        self._dirty = False
        self.saves += 1
        return True


class ProgressStore:
    """进度: 只记 'done'; 失败/未完成的页不写, 下次仍会重抓。"""

    def __init__(self, path, interval=PROGRESS_INTERVAL):
        raw = load_json(path, {})
        if not isinstance(raw, dict):
            raw = {}
        self.done = {k: v for k, v in raw.items() if not k.startswith("__")}
        self.last_failed = raw.get("__failed__", {}) if isinstance(raw.get("__failed__"), dict) else {}
        self.unknown = raw.get("__unknown__", []) if isinstance(raw.get("__unknown__"), list) else []
        self.path = path
        self.interval = interval
        self._last = 0.0
        self._dirty = False

    def is_done(self, key):
        return self.done.get(key) == "done"

    def mark_done(self, key):
        self.done[key] = "done"
        self.last_failed.pop(key, None)
        self._dirty = True

    def mark_failed(self, key, reason):
        self.last_failed[key] = str(reason)[:200]
        self._dirty = True

    def save(self, force=False):
        now = time.time()
        if not self._dirty and not force:
            return False
        if not force and (now - self._last) < self.interval:
            return False
        payload = dict(self.done)
        payload["__failed__"] = self.last_failed
        payload["__unknown__"] = self.unknown
        save_json_atomic(self.path, payload)
        self._last = now
        self._dirty = False
        return True


# ======================== 浏览器 ========================
def find_chromium():
    ms_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright")
    if os.path.isdir(ms_dir):
        candidates = _glob.glob(os.path.join(ms_dir, "chromium-*/chrome-win64/chrome.exe"))
        if candidates:
            return max(candidates)
    return None


def launch_kwargs():
    kw = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-setuid-sandbox",
                 "--disable-blink-features=AutomationControlled",
                 "--disable-dev-shm-usage", "--disable-gpu"],
    }
    exe = find_chromium()
    if exe:
        kw["executable_path"] = exe
    return kw


async def install_blocker(page, block=True):
    """拦截图片/字体/样式/第三方统计, 只保留 HTML 与脚本。"""
    if not block:
        return None

    async def handler(route):
        try:
            req = route.request
            url = req.url
            if req.resource_type in BLOCK_RESOURCE_TYPES or any(h in url for h in BLOCK_HOSTS):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    await page.route("**/*", handler)
    return handler


# ======================== 任务与结果 ========================
class Task:
    __slots__ = ("kind", "role", "slug", "region", "tier", "key", "url")

    def __init__(self, kind, role, slug, region=None, tier=None, url=""):
        self.kind = kind
        self.role = role
        self.slug = slug
        self.region = region
        self.tier = tier
        self.url = url
        if kind == "counter":
            self.key = f"counter/{role}/{slug}"
        elif kind == "build":
            self.key = f"build/{role}/{slug}"
        else:
            self.key = f"tier/{role}/{region}/{tier}"

    def tag(self):
        if self.kind == "counter":
            return f"counter/{self.role}/{self.slug}/{self.region}/{self.tier}"
        if self.kind == "build":
            return f"build/{self.role}/{self.slug}"
        return f"tier/{self.role}/{self.region}/{self.tier}"

    def __repr__(self):
        return f"<Task {self.tag()}>"


class Result:
    __slots__ = ("task", "status", "data", "reason", "attempts", "elapsed")

    def __init__(self, task, status, data=None, reason="", attempts=1, elapsed=0.0):
        self.task = task
        self.status = status          # ok | nodata | fail
        self.data = data
        self.reason = reason
        self.attempts = attempts
        self.elapsed = elapsed


class Stats:
    def __init__(self, total):
        self.total = total
        self.ok = 0
        self.nodata = 0
        self.fail = 0
        self.retries = 0
        self.done = 0
        self.t0 = time.time()

    def bump(self, res):
        self.done += 1
        self.retries += max(0, res.attempts - 1)
        if res.status == "ok":
            self.ok += 1
        elif res.status == "nodata":
            self.nodata += 1
        else:
            self.fail += 1

    def pct(self):
        return 100.0 * self.done / self.total if self.total else 100.0

    def eta(self):
        if not self.done:
            return "-"
        per = (time.time() - self.t0) / self.done
        remain = max(0.0, (self.total - self.done) * per)
        return f"{remain/60:.1f}m"

    def summary(self):
        return (f"{self.done}/{self.total} ({self.pct():.1f}%) "
                f"ok={self.ok} nodata={self.nodata} fail={self.fail} "
                f"retries={self.retries} elapsed={time.time()-self.t0:.1f}s eta={self.eta()}")


# ======================== 抓取核心 ========================
class Scraper:
    def __init__(self, args, name_to_key, champ_by_slug):
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.name_to_key = name_to_key
        self.champ_by_slug = champ_by_slug
        self.page_timeout = args.page_timeout
        self.retries = max(1, args.retries)
        self.min_delay = args.min_delay
        self.block = not args.no_block
        self.rng = random.Random(args.seed)
        self.stats = {}
        # 连续被拦截时的全局退避(避免 8 个并发一起重试, 把限流越拖越久)
        self._blocked_streak = 0
        self._pause_until = 0.0
        self.pauses = 0

    def _note_blocked(self):
        self._blocked_streak += 1
        if self._blocked_streak >= 3:
            idx = self.pauses
            cooldown = min(60.0, 8.0 * (2 ** min(3, idx)))
            self._pause_until = max(self._pause_until, time.time() + cooldown)
            self.pauses += 1
            self._blocked_streak = 0
            print(f"  [限流] 连续 3 页被 403/验证页拦截, 全局暂停 {cooldown:.0f}s "
                  f"(第 {self.pauses} 次) ...")

    def _note_ok(self):
        self._blocked_streak = 0

    async def _wait_if_paused(self):
        while True:
            remain = self._pause_until - time.time()
            if remain <= 0:
                return
            await asyncio.sleep(min(remain, 5.0))

    async def _new_page(self, ctx):
        page = await ctx.new_page()
        page.set_default_timeout(self.page_timeout * 1000)
        await install_blocker(page, self.block)
        return page

    async def _probe(self, page):
        try:
            return await page.evaluate(PROBE_JS)
        except Exception:
            return {"title": "", "head": "", "hasMain": False}

    async def fetch(self, task, ctx, page):
        """抓一个页面, 内部完成重试。返回 Result。"""
        t0 = time.time()
        last_reason = ""
        page_ref = page

        for attempt in range(1, self.retries + 1):
            await self._wait_if_paused()
            if self.min_delay:
                await asyncio.sleep(self.rng.uniform(0, self.min_delay))
            try:
                await page_ref.goto(task.url, wait_until="domcontentloaded",
                                    timeout=self.page_timeout * 1000)
                probe = await self._probe(page_ref)
                if is_blocked_text(probe.get("head", ""), probe.get("title", "")):
                    self._note_blocked()
                    last_reason = "blocked (403/challenge)"
                    if attempt < self.retries:
                        await asyncio.sleep(backoff_delay(attempt, self.rng, blocked=True))
                        continue
                    return Result(task, "fail", reason=last_reason,
                                  attempts=attempt, elapsed=time.time() - t0)

                waiter = {"counter": WAIT_COUNTER_JS, "build": WAIT_BUILD_JS,
                          "tier": WAIT_TIER_JS}[task.kind]
                try:
                    handle = await page_ref.wait_for_function(waiter, timeout=self.page_timeout * 1000)
                    state = await handle.json_value()
                except Exception:
                    # 等待超时: 再探一次, 区分"被拦截"和"真的没数据"
                    probe2 = await self._probe(page_ref)
                    if is_blocked_text(probe2.get("head", ""), probe2.get("title", "")):
                        self._note_blocked()
                        last_reason = "blocked after wait (403/challenge)"
                        if attempt < self.retries:
                            await asyncio.sleep(backoff_delay(attempt, self.rng, blocked=True))
                            continue
                        return Result(task, "fail", reason=last_reason,
                                      attempts=attempt, elapsed=time.time() - t0)
                    last_reason = "wait timeout (no data marker)"
                    if attempt < self.retries:
                        await asyncio.sleep(backoff_delay(attempt, self.rng))
                        page_ref = await self._recycle(ctx, page_ref)
                        continue
                    return Result(task, "fail", reason=last_reason,
                                  attempts=attempt, elapsed=time.time() - t0)

                if state == "nodata":
                    self._note_ok()
                    return Result(task, "nodata", reason="insufficient samples",
                                  attempts=attempt, elapsed=time.time() - t0)

                extractor = {"counter": EXTRACT_COUNTER_JS, "build": EXTRACT_BUILD_JS,
                             "tier": EXTRACT_TIER_JS}[task.kind]
                payload = await page_ref.evaluate(extractor)

                if task.kind == "counter":
                    rows = finalize_counter_rows(payload.get("rows"), self.name_to_key)
                    if rows:
                        self._note_ok()
                        return Result(task, "ok", rows, attempts=attempt,
                                      elapsed=time.time() - t0)
                    if payload.get("nodata"):
                        self._note_ok()
                        return Result(task, "nodata", reason="insufficient samples",
                                      attempts=attempt, elapsed=time.time() - t0)
                    last_reason = f"no rows (DOM rows={len(payload.get('rows') or [])})"
                elif task.kind == "tier":
                    rows = finalize_tier_rows(payload.get("rows"), self.champ_by_slug,
                                              self.name_to_key)
                    if rows:
                        self._note_ok()
                        return Result(task, "ok", rows, attempts=attempt,
                                      elapsed=time.time() - t0)
                    last_reason = f"no tier rows (DOM rows={len(payload.get('rows') or [])})"
                else:
                    parsed = parse_build_payload(payload)
                    has_data = bool(parsed["runes"].get("perks") or parsed["core_items"]
                                    or parsed["skill_order"] or parsed["spells"])
                    if has_data:
                        self._note_ok()
                        return Result(task, "ok", parsed, attempts=attempt,
                                      elapsed=time.time() - t0)
                    last_reason = "build page had no runes/items/skills"

            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_reason = f"{e.__class__.__name__}: {str(e)[:120]}"
                # 页面可能已崩, 换一个新标签页继续
                try:
                    page_ref = await self._recycle(ctx, page_ref)
                except Exception:
                    pass

            if attempt < self.retries:
                await asyncio.sleep(backoff_delay(attempt, self.rng))

        return Result(task, "fail", reason=last_reason, attempts=self.retries,
                      elapsed=time.time() - t0)

    async def _recycle(self, ctx, page):
        try:
            await page.close()
        except Exception:
            pass
        return await self._new_page(ctx)

    async def run_batch(self, tasks, ctx, store, progress, on_result, label):
        """并发跑一批任务。"""
        if not tasks:
            return []
        stats = Stats(len(tasks))
        self.stats[label] = stats
        queue = asyncio.Queue()
        for t in tasks:
            queue.put_nowait(t)
        results = []
        lock = asyncio.Lock()

        async def worker():
            page = await self._new_page(ctx)
            try:
                while True:
                    try:
                        task = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    res = await self.fetch(task, ctx, page)
                    async with lock:
                        stats.bump(res)
                        results.append(res)
                        on_result(res)
                        if store is not None:
                            store.maybe_save()
                        if progress is not None:
                            progress.save()
                        self._log(res, stats)
                        if stats.done % 25 == 0:
                            print(f"  [{label}] {stats.summary()}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        n = max(1, min(self.args.concurrency, len(tasks)))
        await asyncio.gather(*[worker() for _ in range(n)])
        return results

    def _log(self, res, stats):
        tag = res.task.tag()
        if res.status == "ok":
            n = len(res.data) if isinstance(res.data, list) else (
                len(res.data.get("core_items", [])) if isinstance(res.data, dict) else 0)
            print(f"  + {tag}: {n} 条  ({res.elapsed:.1f}s)")
        elif res.status == "nodata":
            print(f"  - {tag}: {res.reason}")
        else:
            print(f"  ! {tag}: {res.reason} (尝试 {res.attempts} 次)")


# ======================== 分路名单解析 ========================
def load_lane_pool(champs):
    """解析 lanes.txt, 返回 {role: set(slug)} 和 {slug: [role, ...]}。"""
    lanes_path = os.path.join(HERE, "lanes.txt")
    if not os.path.exists(lanes_path):
        return None, None

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
    text = text.lstrip("\ufeff")

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

        parts = re.split(r'\d+\s*[\.\)、]\s*', line)
        for part in parts:
            part = part.strip()
            if not part:
                continue
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

    slug_to_roles = {}
    for role, slugs in pool.items():
        for slug in slugs:
            slug_to_roles.setdefault(slug, []).append(role)

    print(f"[*] lanes.txt: {sum(len(v) for v in pool.values())} 条英雄-分路映射")
    return pool, slug_to_roles


# ======================== 数据写入回调 ========================
def make_counter_applier(data):
    def apply(res):
        t = res.task
        if res.status != "ok":
            return
        data.setdefault(t.role, {}).setdefault(t.slug, {}) \
            .setdefault(t.region, {})[t.tier] = res.data
    return apply


def make_build_applier(data):
    def apply(res):
        t = res.task
        if res.status != "ok":
            return
        data.setdefault(t.role, {})[t.slug] = res.data
    return apply


def make_tier_applier(data):
    def apply(res):
        t = res.task
        if res.status != "ok":
            return
        data.setdefault(t.role, {}).setdefault(t.region, {})[t.tier] = res.data
    return apply


# ======================== 主流程 ========================
async def async_main(args):
    if args.offline:
        champs = load_json(OUT_CHAMPS, None)
        if not champs:
            sys.exit(f"[!] --offline 需要本地 {OUT_CHAMPS} (可先联网跑一次)")
        print(f"[*] 离线模式: 使用本地 champions.json ({len(champs)} 英雄)")
    else:
        champs = fetch_champions()
        save_json_atomic(OUT_CHAMPS, champs)
        print(f"[+] 英雄映射 -> {OUT_CHAMPS}")
    by_key = {c["key"]: c for c in champs}
    champ_by_slug = {c["slug"]: c for c in champs}

    name_to_key = {}
    for c in champs:
        if c["name"]:
            name_to_key[c["name"]] = c["key"]
        if c["title"]:
            name_to_key[c["title"]] = c["key"]

    roles = list(ROLES.values())
    if args.roles:
        roles = [ROLES.get(r.strip(), r.strip()) for r in args.roles.split(",")]

    only = set(s.strip() for s in args.only.split(",")) if args.only else {"counter", "build", "tier"}
    progress = ProgressStore(PROGRESS)

    lane_pool, slug_to_roles = load_lane_pool(champs)

    if args.fast:
        active_regions = {"global": "global", "cn": "cn"}
        active_tiers = {"diamond": "diamond", "master": "master"}
        use_lane_filter = True
        print("[*] 快速模式: Diamond+Master, Global+CN, 仅正常分路")
    elif args.full:
        active_regions = dict(REGIONS)
        active_tiers = dict(TIERS)
        use_lane_filter = False
        print("[*] 全量模式: 所有地区+段位+英雄")
    else:
        active_regions = {"global": "global", "cn": "cn"}
        active_tiers = {"diamond": "diamond", "master": "master"}
        use_lane_filter = True
        print("[*] 默认模式: Diamond+Master, Global+CN, 仅正常分路 (用 --full 全量抓取)")

    # 英雄列表
    role_heroes = {}
    forced = [s.strip().lower() for s in args.heroes.split(",")] if args.heroes else None
    for role in roles:
        if forced:
            heroes = [c for c in champs if c["slug"] in forced]
        elif use_lane_filter and lane_pool and not args.limit:
            heroes = [c for c in champs if c["slug"] in lane_pool.get(role, set())]
        elif use_lane_filter and lane_pool:
            heroes = [c for c in champs if c["slug"] in lane_pool.get(role, set())]
        else:
            heroes = list(champs)
        if args.limit:
            heroes = heroes[:args.limit]
        role_heroes[role] = heroes
        how = "指定" if forced else ("已过滤" if use_lane_filter and lane_pool else "全部")
        print(f"  {role}: {len(heroes)} 个英雄 ({how})")

    if forced and not any(role_heroes.values()):
        print(f"[!] --heroes 指定的英雄都不在 champions.json 里: {forced}")
        return

    # 元数据(build 需要; --only meta 可单独重生成)
    if ({"build"} & only or "meta" in only) and not args.no_meta and not args.offline:
        try:
            meta = fetch_meta()
            save_json_atomic(OUT_META, meta)
            print(f"[+] 元数据 -> {OUT_META}")
        except Exception as e:
            print(f"[!] 元数据下载失败(继续抓取): {e}")
            if only == {"meta"}:
                return
    if only == {"meta"}:
        print("[*] 仅更新 meta.json，完成(未抓取任何页面)。")
        return

    scraper = Scraper(args, name_to_key, champ_by_slug)

    # ---------- 任务清单 ----------
    # --fresh: 忽略进度(全部重抓), 但只在"全量范围"下才清空已有数据;
    # 若同时用了 --heroes/--limit/--only/--roles 等局部范围, 保留旧数据, 只覆盖本次抓到的条目。
    partial_scope = bool(args.heroes or args.limit or args.only or args.roles or args.debug)
    wipe = args.fresh and not partial_scope
    counter_tasks, build_tasks, tier_tasks = [], [], []
    if "counter" in only:
        for role in roles:
            for c in role_heroes.get(role, []):
                if not args.fresh and progress.is_done(f"counter/{role}/{c['slug']}"):
                    continue
                for region in active_regions.values():
                    for tier in active_tiers.values():
                        counter_tasks.append(Task("counter", role, c["slug"], region, tier,
                                                  url_counter(scraper.base, c["slug"], role, region, tier)))
    if "build" in only:
        for role in roles:
            for c in role_heroes.get(role, []):
                if not args.fresh and progress.is_done(f"build/{role}/{c['slug']}"):
                    continue
                build_tasks.append(Task("build", role, c["slug"], url=url_build(scraper.base, c["slug"], role)))
    if "tier" in only:
        for role in roles:
            for region in active_regions.values():
                for tier in active_tiers.values():
                    if not args.fresh and progress.is_done(f"tier/{role}/{region}/{tier}"):
                        continue
                    tier_tasks.append(Task("tier", role, "", region, tier,
                                           url_tier(scraper.base, role, region, tier)))

    total_tasks = len(counter_tasks) + len(build_tasks) + len(tier_tasks)
    if not total_tasks:
        print("[*] 没有需要抓取的页面(全部已完成)。加 --fresh 可强制重抓。")
        return
    print(f"[*] 待抓 {total_tasks} 页 "
          f"(counter={len(counter_tasks)} build={len(build_tasks)} tier={len(tier_tasks)}), "
          f"并发={args.concurrency}, 单页超时={args.page_timeout}s, 重试={args.retries}")

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(**launch_kwargs())
    ctx = await browser.new_context(user_agent=UA, locale="zh-CN",
                                    viewport={"width": 1600, "height": 900})

    counter_data = {} if wipe else (load_json(OUT_COUNTER, {}) or {})
    build_data = {} if wipe else (load_json(OUT_BUILD, {}) or {})
    tier_data = {} if wipe else (load_json(OUT_TIER, {}) or {})
    if args.fresh and partial_scope and any(os.path.exists(p) for p in (OUT_COUNTER, OUT_BUILD, OUT_TIER)):
        print("[*] --fresh + 局部范围: 保留已有数据, 只覆盖本次抓到的条目 "
              "(想要清空重来请用全量范围, 例如 python scrape.py --fresh)")
    counter_store = JsonStore(OUT_COUNTER, counter_data) if counter_tasks else None
    build_store = JsonStore(OUT_BUILD, build_data) if build_tasks else None
    tier_store = JsonStore(OUT_TIER, tier_data) if tier_tasks else None

    # 通用: 跑一批任务 -> 失败补抓 -> 按 key 汇总进度 -> 落盘
    async def run_section(tasks, store, apply, label, tip=""):
        if not tasks:
            return []
        print(f"\n========== 抓取 {label} ==========")
        on_result = make_applier_chain(apply, store)
        all_results, pending = [], list(tasks)
        rounds_left = max(0, args.repair_rounds)
        first = True
        while True:
            results = await scraper.run_batch(pending, ctx, store, progress, on_result,
                                              label if first else f"{label}-repair")
            first = False
            all_results.extend(results)
            pending = [r.task for r in results if r.status == "fail"]
            if not pending or rounds_left <= 0:
                break
            rounds_left -= 1
            print(f"  [补抓] {label}: 还有 {len(pending)} 页失败, 2 秒后补抓 ...")
            await asyncio.sleep(2.0)
        # 只有该 key 下所有页面都成功(或确认"样本不足")才写进度;
        # 失败的 key 不写进度, 本轮结束会补抓, 下次运行仍会重抓。
        pending_keys = {t.key for t in pending}
        by_key = {}
        for r in all_results:
            by_key.setdefault(r.task.key, []).append(r)
        for key, rs in by_key.items():
            if key in pending_keys:
                progress.mark_failed(key, rs[-1].reason)
            else:
                progress.mark_done(key)
        store.maybe_save(force=True)
        progress.save(force=True)
        ok = sum(1 for r in all_results if r.status == "ok")
        nd = sum(1 for r in all_results if r.status == "nodata")
        print(f"[+] {label} -> {store.path} (成功 {ok} 页, 无数据 {nd} 页, "
              f"失败 {len(pending)} 页)")
        if tip:
            print(tip)
        return all_results

    if counter_tasks:
        await run_section(counter_tasks, counter_store,
                          make_counter_applier(counter_data), "克制(counter)")
    if build_tasks:
        await run_section(build_tasks, build_store,
                          make_build_applier(build_data), "出装(build)")
    if tier_tasks:
        await run_section(tier_tasks, tier_store,
                          make_tier_applier(tier_data), "排名(tier)",
                          tip="    提示: tier_data.json 不含对局数; 需要完整排名请再跑 python gen_tier.py")

    await ctx.close()
    await browser.close()
    await pw.stop()

    print(f"\n[+] 全部完成! 用时 {time.time() - START_TS:.1f}s")


def make_applier_chain(apply, store):
    def wrapper(res):
        if res.status == "ok":
            apply(res)
            if store is not None:
                store.touch()
    return wrapper


START_TS = time.time()


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", metavar="SLUG", help="只抓该英雄 counter+build 并 dump 调试数据")
    ap.add_argument("--limit", type=int, help="每分路只抓前 N 个英雄")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略进度全部重抓; 全量范围下同时清空旧数据, 局部范围(--heroes/--limit/--only/--roles)下保留旧数据")
    ap.add_argument("--only", help="只抓某类: counter,build,tier (逗号分隔; meta=只更新元数据)")
    ap.add_argument("--roles", help="只抓这些分路, 逗号分隔")
    ap.add_argument("--heroes", help="只抓这些英雄(slug), 逗号分隔")
    ap.add_argument("--fast", action="store_true",
                    help="快速模式: 仅 Diamond+Master, 仅 Global+CN, 严格按 lanes.txt 分路")
    ap.add_argument("--full", action="store_true", help="全量模式: 所有地区+段位+所有英雄组合")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"并发标签页数 (默认 {DEFAULT_CONCURRENCY})")
    ap.add_argument("--page-timeout", type=float, default=DEFAULT_PAGE_TIMEOUT,
                    help=f"单页超时秒数 (默认 {DEFAULT_PAGE_TIMEOUT})")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"单页最多尝试次数 (默认 {DEFAULT_RETRIES})")
    ap.add_argument("--repair-rounds", type=int, default=1,
                    help="整轮补抓次数 (默认 1)")
    ap.add_argument("--min-delay", type=float, default=DEFAULT_MIN_DELAY,
                    help=f"页面请求间最小随机间隔秒 (默认 {DEFAULT_MIN_DELAY})")
    ap.add_argument("--base-url", default=DEFAULT_BASE, help="站点根地址(测试可指向本地 mock)")
    ap.add_argument("--no-block", action="store_true", help="不拦截图片/样式等资源")
    ap.add_argument("--no-meta", action="store_true", help="跳过 Data Dragon 元数据下载")
    ap.add_argument("--data-dir", default=HERE, help="数据输出目录(默认脚本所在目录)")
    ap.add_argument("--offline", action="store_true",
                    help="离线: 不访问 Data Dragon, 直接用本地 champions.json (测试用)")
    ap.add_argument("--seed", type=int, default=None, help="随机种子(测试用)")
    return ap


def main():
    global START_TS
    START_TS = time.time()
    args = build_parser().parse_args()
    if args.data_dir:
        set_data_dir(args.data_dir)

    # ---- debug: 单英雄, 顺序跑一遍并 dump ----
    if args.debug:
        if args.offline:
            champs = load_json(OUT_CHAMPS, None)
            if not champs:
                sys.exit(f"[!] --offline 需要本地 {OUT_CHAMPS}")
        else:
            champs = fetch_champions()
            save_json_atomic(OUT_CHAMPS, champs)
        by_key = {c["key"]: c for c in champs}
        champ_by_slug = {c["slug"]: c for c in champs}
        name_to_key = {}
        for c in champs:
            if c["name"]:
                name_to_key[c["name"]] = c["key"]
            if c["title"]:
                name_to_key[c["title"]] = c["key"]
        slug = args.debug.lower()
        debug_role = "support"
        try:
            lane_pool, slug_to_roles = load_lane_pool(champs)
            roles_of = slug_to_roles.get(slug, [])
            if roles_of:
                debug_role = roles_of[0]
        except Exception:
            pass
        print(f"[*] DEBUG: {slug} (角色={debug_role})")
        return _debug_run(args, slug, debug_role, name_to_key, champ_by_slug, by_key)

    asyncio.run(async_main(args))


def _goto_with_retry(page, url, timeout_ms, attempts=2):
    """debug 模式用: 打开页面并跳过 CloudFront/Cloudflare 拦截页, 失败自动退避重试。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            probe = page.evaluate(PROBE_JS)
            if is_blocked_text(probe.get("head", ""), probe.get("title", "")):
                raise RuntimeError("返回拦截页(" + (probe.get("title") or "CloudFront/Cloudflare") + ")")
            return True, probe
        except Exception as e:
            last = e
            if i < attempts:
                d = backoff_delay(i, blocked=True)
                print(f"      (第 {i} 次失败: {str(e)[:70]} -> {d:.1f}s 后重试)")
                time.sleep(d)
    return False, last


def _debug_run(args, slug, role, name_to_key, champ_by_slug, by_key):
    from playwright.sync_api import sync_playwright
    urls = [
        ("counter", url_counter(args.base_url, slug, role, "global", "diamond")),
        ("build", url_build(args.base_url, slug, role)),
        ("tier", url_tier(args.base_url, role, "global", "diamond")),
    ]
    waiter = {"counter": WAIT_COUNTER_JS, "build": WAIT_BUILD_JS, "tier": WAIT_TIER_JS}
    extractor = {"counter": EXTRACT_COUNTER_JS, "build": EXTRACT_BUILD_JS, "tier": EXTRACT_TIER_JS}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs())
        ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                  viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        if not args.no_block:
            page.route("**/*", lambda route: _sync_block(route, args))
        for kind, url in urls:
            t0 = time.time()
            print(f"[*] {kind}: {url}")
            try:
                ok, probe = _goto_with_retry(page, url, int(args.page_timeout * 1000))
                if not ok:
                    print(f"[!] {kind} 打开失败: {str(probe)[:120]}")
                    print("    提示: op.gg 对高频访问会返回 403/验证页, 等几分钟再试或降低 --concurrency")
                    continue
                try:
                    page.wait_for_function(waiter[kind], timeout=args.page_timeout * 1000)
                except Exception:
                    print(f"[!] {kind} 等待数据超时 ({time.time()-t0:.1f}s)")
                payload = page.evaluate(extractor[kind])
                if kind == "counter":
                    rows = finalize_counter_rows(payload.get("rows"), name_to_key)
                    print(f"[+] counter: {len(rows)} 条 ({time.time()-t0:.1f}s)")
                    for x in rows[:10]:
                        opp = by_key.get(x["opp"], {}).get("name", x["opp"])
                        print(f"    vs {opp}: 胜率 {x['win']}%  ({x['games']} 局)")
                    save_json_atomic(os.path.join(args.data_dir, f"debug_{slug}_counter.json"), rows)
                elif kind == "build":
                    b = parse_build_payload(payload)
                    print(f"[+] build: {json.dumps(b, ensure_ascii=False)[:400]} ({time.time()-t0:.1f}s)")
                    save_json_atomic(os.path.join(args.data_dir, f"debug_{slug}_build.json"), b)
                else:
                    rows = finalize_tier_rows(payload.get("rows"), champ_by_slug, name_to_key)
                    print(f"[+] tier: {len(rows)} 项 ({time.time()-t0:.1f}s)")
                    for x in rows[:8]:
                        nm = by_key.get(x["key"], {}).get("name", x["key"])
                        print(f"    #{x['rank']} {nm}: 胜率 {x['win']}")
                    save_json_atomic(os.path.join(args.data_dir, f"debug_{slug}_tier.json"), rows)
            except Exception as e:
                print(f"[!] {kind} 失败: {e.__class__.__name__}: {e}")
        ctx.close()
        browser.close()


def _sync_block(route, args):
    try:
        req = route.request
        if req.resource_type in BLOCK_RESOURCE_TYPES or any(h in req.url for h in BLOCK_HOSTS):
            route.abort()
        else:
            route.continue_()
    except Exception:
        try:
            route.continue_()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[中断] 进度已保存, 再次运行可续抓。")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
