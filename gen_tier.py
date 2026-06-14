#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 counter_data.json 推导英雄排名 (替代失效的 OP.GG tier 抓取)。

逻辑: 对每个角色, 统计每个英雄作为"克制方"时的平均胜率, 降序排列。
"""

import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
COUNTER_FILE = os.path.join(HERE, "counter_data.json")
TIER_FILE = os.path.join(HERE, "tier_data.json")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    counter = load_json(COUNTER_FILE)
    tier = {}

    for role in counter:
        # 统计每个对手英雄的胜率和出场数
        # counter[role][slug][region][tier] = [{opp, win, games}, ...]
        hero_stats = defaultdict(lambda: {"wins": 0, "games": 0, "appearances": 0})

        for slug, regions in counter[role].items():
            for region, tiers in regions.items():
                for tier_name, rows in tiers.items():
                    for row in rows:
                        opp_key = row.get("opp")
                        if not opp_key:
                            continue
                        win_rate = float(row.get("win", 50))
                        games = int(row.get("games", 0))
                        hero_stats[opp_key]["wins"] += win_rate * games
                        hero_stats[opp_key]["games"] += games
                        hero_stats[opp_key]["appearances"] += 1

        # 计算加权平均胜率 (至少出现5次才纳入)
        rankings = []
        for opp_key, stats in hero_stats.items():
            if stats["games"] >= 500:  # 至少500局样本, 过滤冷门非主流选择
                avg_win = stats["wins"] / stats["games"]
                rankings.append({
                    "key": opp_key,
                    "win": round(avg_win, 2),
                    "games": stats["games"],
                    "appearances": stats["appearances"],
                })

        # 按胜率降序
        rankings.sort(key=lambda x: (-x["win"], -x["games"]))
        for i, r in enumerate(rankings):
            r["rank"] = i + 1

        # 存入 tier 结构 (兼容原格式)
        tier[role] = {}
        for region in ["global", "cn"]:
            tier[role][region] = {}
            for tier_name in ["diamond", "master"]:
                tier[role][region][tier_name] = rankings

    save_json(tier, TIER_FILE)

    for role in tier:
        rankings = tier[role].get("global", {}).get("diamond", [])
        if rankings:
            print(f"  {role}: Top5 = {[r['key'] for r in rankings[:5]]}")
    print(f"\n[+] tier_data.json 已生成 ({len(tier)} 个分路)")


if __name__ == "__main__":
    main()
