#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量 AI 生成连招, 填入 build_data.json（并发版）。

用法:
    python gen_combos.py                  # 并发生成所有缺失/兜底的连招（默认并发 16）
    python gen_combos.py --concurrency 32 # 并发数（上限 800，实际按英雄数收敛）
    python gen_combos.py --dry            # 预览，不调用 API
    python gen_combos.py --limit 3        # 只处理前 3 个（验证用）
    python gen_combos.py --force          # 连已有 AI 连招也重新生成
    python gen_combos.py --model deepseek-v4-pro
    python gen_combos.py --data-dir out   # 输出到指定目录（默认脚本目录）

模型说明:
    该账号可用模型只有 deepseek-flash / deepseek-v4-pro（见 ai_common.MODEL_CHAIN）。
    旧版写的 DeepSeek-V4.1-Flash-1017 会被 400 拒绝，现在会自动降级并在结束时汇报。

提速（193 个英雄实测）:
    串行旧版 663s -> 并发 8: 63s -> 并发 16: 32s -> 并发 32: 17s -> 并发 64: 10s。
    线程池 + urllib 实现，无需额外依赖；单条失败只影响自己，重试/换模型互不干扰。
"""

import argparse
import os
import sys
import threading
import time

import ai_common as ai
import combo_text as ct

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_FILE = os.path.join(HERE, "build_data.json")
CHAMPS_FILE = os.path.join(HERE, "champions.json")

ROLE_NAMES = {
    "top": "上路", "jungle": "打野", "mid": "中路", "adc": "下路", "support": "辅助",
}

FALLBACK_NOTES = ("基础技能连招", "大招起手进阶连招")
PLACEHOLDER_MARKERS = ("key sequence here", "use actual champion", "use actual")


# ======================== 纯函数 ========================
def build_prompt(champion_name, role_cn, skills, skill_order):
    return f"""You are a League of Legends expert with knowledge of ALL champion reworks up to 2025. Use CURRENT patch abilities.

Champion: {champion_name} (English name may help identify correct abilities)
Role: {role_cn}
Skill max order: {skills}
First 6 levels: {skill_order}

What are the core combos? Reply with ONLY valid JSON, no markdown, no other text:
{{"combos":[{{"type":"basic","sequence":"key sequence here","note":"when to use"}},{{"type":"medium","sequence":"key sequence here","note":"when to use"}}]}}"""


def parse_combos(text):
    """从模型输出解析 combos；格式不对/是占位模板则返回 None。"""
    obj = ai.extract_json_object(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("combos"), list):
        return None
    out = []
    for c in obj["combos"]:
        if not isinstance(c, dict):
            continue
        seq = (c.get("sequence") or "").strip()
        if not seq:
            continue
        out.append({
            "type": c.get("type") or "basic",
            "sequence": seq,
            "note": (c.get("note") or "").strip(),
        })
    if not out:
        return None
    if is_placeholder(out):
        return None
    # 归一化: AA/auto attack -> 平A，去掉英文括注，箭头统一为 →
    cleaned = [ct.normalize_combo(c) for c in out]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return None
    # 前端最多展示 4 条，多出来的截断，避免一条连招塞 6~8 条
    return cleaned[:4]


def is_placeholder(combos):
    """模型没思考，直接把 prompt 里的示例抄回来。"""
    for c in combos or []:
        seq = (c.get("sequence") or "").lower()
        if any(m in seq for m in PLACEHOLDER_MARKERS):
            return True
    return False


def is_fallback_combos(combos):
    """是不是 fallback_combo() 生成的兜底连招（需要重新用 AI 生成）。"""
    if not combos:
        return False
    notes = [(c.get("note") or "") for c in combos]
    return any(n in FALLBACK_NOTES for n in notes)


def fallback_combo(skills_str):
    """API 失败时从技能优先级生成简单连招。"""
    skills = [s.strip() for s in (skills_str or "").split(">") if s.strip()]
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


def needs_generation(node, force=False):
    """该英雄是否需要（重新）生成连招。"""
    if not node.get("skill_master"):
        return False
    combos = node.get("combos") or []
    if force:
        return True
    if not combos:
        return True
    return is_fallback_combos(combos)


# ======================== 主流程 ========================
def build_parser():
    ap = argparse.ArgumentParser(description="批量 AI 生成英雄连招（并发）")
    ap.add_argument("--dry", action="store_true", help="只预览，不调用 API")
    ap.add_argument("--limit", type=int, help="只处理前 N 个英雄")
    ap.add_argument("--concurrency", type=int, default=ai.DEFAULT_CONCURRENCY,
                    help=f"并发请求数（默认 {ai.DEFAULT_CONCURRENCY}，上限 {ai.MAX_CONCURRENCY}，"
                         f"实际按英雄数收敛）")
    ap.add_argument("--force", action="store_true", help="已有 AI 连招也重新生成")
    ap.add_argument("--model", help="指定模型，逗号分隔可给多个（默认 deepseek-flash -> deepseek-v4-pro）")
    ap.add_argument("--api-url", default=ai.DEFAULT_API_URL, help="API 地址（测试用）")
    ap.add_argument("--data-dir", default=HERE, help="数据目录（默认脚本所在目录）")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="每次请求前的额外等待秒数（并发时一般不用；默认 0）")
    ap.add_argument("--retries", type=int, default=3, help="限流/服务端错误的单模型重试次数")
    ap.add_argument("--max-tokens", type=int, default=800, help="单次回复 token 上限")
    return ap


def main():
    args = build_parser().parse_args()
    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)
    build_file = os.path.join(data_dir, "build_data.json")

    if not os.path.exists(build_file):
        sys.exit(f"错误: 找不到 {build_file}，请先运行 scrape.py")

    api_key = ai.load_api_key(HERE)
    if not api_key:
        sys.exit("错误: 未找到 DEEPSEEK_API_KEY。请在 .env 文件中配置（参考 .env.example）。")

    models = tuple(s.strip() for s in args.model.split(",") if s.strip()) if args.model \
        else ai.MODEL_CHAIN

    build = ai.load_json(build_file)
    champs = ai.load_json(CHAMPS_FILE)
    slug_to_name = {c["slug"]: c["name"] for c in champs}

    tasks = []
    for role in build:
        for slug, node in build[role].items():
            if needs_generation(node, args.force):
                tasks.append((role, slug, slug_to_name.get(slug, slug),
                              ">".join(node.get("skill_master") or []),
                              "".join((node.get("skill_order") or [])[:6])))
    if args.limit:
        tasks = tasks[:args.limit]

    n = len(tasks)
    print(f"[*] 待生成: {n} 个英雄（模型链: {' -> '.join(models)}，Key: {ai.mask_key(api_key)}）")
    if not n:
        print("[*] 全部已有连招，无需生成（--force 可强制重生成）")
        return 0
    if args.dry:
        for role, slug, name, skills, order in tasks[:10]:
            print(f"  [{role}] {name}: skills={skills} order={order}")
        if n > 10:
            print(f"  ... 共 {n} 个")
        return 0

    workers = ai.clamp_workers(args.concurrency, n)
    if workers > 32:
        print(f"[!] 并发 {workers} 偏高，可能触发 API 限流（429 会自动重试，但更慢）；建议 8~32")

    print_lock = threading.Lock()

    def say(msg):
        with print_lock:
            print(msg, flush=True)

    stats = {"ai": 0, "fallback": 0, "fail": 0}
    used_models = {}
    state = {"done": 0, "api_fail": 0, "auth": False}
    t0 = time.time()

    def worker(task):
        role, slug, name, skills, order = task
        ts = time.time()
        prompt = build_prompt(name, ROLE_NAMES.get(role, role), skills, order)
        text, err, model = ai.request_text(
            prompt, api_key=api_key, models=models, api_url=args.api_url,
            max_tokens=args.max_tokens, retries=args.retries,
            log=lambda m: say(f"  [{name}]{m}"))
        return text, err, model, time.time() - ts

    def on_result(idx, task, res):
        role, slug, name, skills, order = task
        if isinstance(res, BaseException):
            text, err, model, elapsed = None, ai.AiError("other", str(res)), None, 0.0
        else:
            text, err, model, elapsed = res

        combos = parse_combos(text) if text else None
        if combos:
            build[role][slug]["combos"] = combos
            stats["ai"] += 1
            state["api_fail"] = 0
            used_models[model] = used_models.get(model, 0) + 1
            head = " | ".join(c["sequence"][:22] for c in combos[:2])
            say(f"[{idx + 1}/{n}] [{role}] {name} OK [{model}] {head}  ({elapsed:.1f}s)")
        else:
            kind = err.kind if err else "parse"
            reason = str(err)[:110] if err else "模型输出无法解析成连招 JSON"
            if kind == "auth":
                say(f"[{idx + 1}/{n}] [{role}] {name} 鉴权失败: {reason}")
                state["auth"] = True
                return False
            fb = fallback_combo(skills)
            if fb:
                build[role][slug]["combos"] = fb
                stats["fallback"] += 1
                say(f"[{idx + 1}/{n}] [{role}] {name} 兜底（{reason}）  ({elapsed:.1f}s)")
            else:
                stats["fail"] += 1
                say(f"[{idx + 1}/{n}] [{role}] {name} 失败（{reason}）  ({elapsed:.1f}s)")
            state["api_fail"] += 1
            if state["api_fail"] >= max(6, workers) and stats["ai"] == 0:
                say(f"\n[!] 连续 {state['api_fail']} 个英雄都没能调通 API，已停止，"
                    f"已保存部分结果（修好配置后重跑会接着补）")
                return False

        state["done"] += 1
        if state["done"] % 30 == 0:
            ai.save_json_atomic(build, build_file)
            say(f"  [SAVE] 已完成 {state['done']}/{n}  "
                f"AI {stats['ai']} · 兜底 {stats['fallback']} · 失败 {stats['fail']}")
        return True

    try:
        _, aborted = ai.parallel_map(tasks, worker, concurrency=workers, on_result=on_result)
    except KeyboardInterrupt:
        print("\n[中断] 保存已生成的部分 ...")
        ai.save_json_atomic(build, build_file)
        return 130

    ai.save_json_atomic(build, build_file)
    dur = time.time() - t0

    if state["auth"]:
        print("\n[!] 鉴权失败：请检查 .env 里的 DEEPSEEK_API_KEY 是否正确/未过期。")
        print("    已生成的部分已保存，修好后重跑会接着补。")
        return 2

    print(f"\n[+] 完成: AI 生成 {stats['ai']} · 兜底 {stats['fallback']} · 失败 {stats['fail']}"
          f"（{dur:.1f}s，并发 {workers}）")
    if used_models:
        print("    使用的模型: " + ", ".join(f"{m}×{c}" for m, c in used_models.items()))
    print(f"    写入 {build_file}")
    if aborted:
        print("[!] 提前中止，剩余英雄未处理；重跑本脚本会接着补。")
        return 4
    if stats["ai"] == 0:
        print("[!] 一条 AI 连招都没生成，检查上面的报错原因（不要用兜底数据出成品）。")
        return 3
    print("    下一步: python trans_combos.py 然后 python build.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
