#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自愈回填：扫描最近 N 天，把 HTML 里缺失（或条数明显不足）的日期自动补上。

存在的理由（2026-10-19）
------------------------
GitHub Actions 的 schedule cron 不可靠：实测本仓库 UTC 01:30 / 06:30 两趟
会被整体推迟 4~5 小时才启动，个别时段直接不触发；而 pipeline 本身在
"LLM 输出 JSON 截断解析失败"或"国内不足 8 条 / 国外不足 5 条"时会 return 1
整天放弃写入。两者叠加的结果就是线上时不时缺一整天（10-17 就是 JSON 解析
失败两次、整天没内容）。

本脚本不替代日常 pipeline，只做兜底：每天跑一趟，找出洞补上。
有洞才抓素材、才调模型；没洞 20 秒内退出，成本近似为零。

用法：
    python auto_heal.py              # 补最近 7 天的洞
    python auto_heal.py --days 3     # 只看最近 3 天
    python auto_heal.py --min-items 10   # 已有但总条数 < 10 也判定为洞
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE not in sys.path:
    sys.path.insert(0, WORKSPACE)

import news_pipeline as np  # noqa: E402
import backfill_news as bf  # noqa: E402

FULL_HTML = bf.FULL_HTML
LITE_HTML = bf.LITE_HTML

DEFAULT_DAYS = 7
DEFAULT_MIN_ITEMS = 10


def existing_days(html_path):
    """返回 {日期: 该天条目数}。解析失败返回空 dict（让上层判定为全洞）。"""
    out = {}
    try:
        with open(html_path, encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        np.log(f"读取失败 {html_path}: {e}")
        return out
    for m in re.finditer(r'date:\s*"(20\d\d-\d\d-\d\d)"', src):
        d = m.group(1)
        out.setdefault(d, 0)
    # 再逐块数条目数，用于判断"有日期但条数太少"
    for m in re.finditer(
        r'\{\s*date:\s*"(20\d\d-\d\d-\d\d)"[\s\S]{0,40000}?items:\s*\[', src
    ):
        d = m.group(1)
        start = m.end()
        depth, end = 1, None
        for j in range(start, min(start + 60000, len(src))):
            ch = src[j]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            continue
        seg = src[start:end]
        out[d] = len(re.findall(r"\btitle:", seg))
    return out


def find_gaps(days_back, min_items, today=None):
    """返回需要补的日期列表（升序）。两个文件取条数较少的那个做判据。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    have_full = existing_days(FULL_HTML)
    have_lite = existing_days(LITE_HTML)
    gaps = []
    for k in range(days_back, -1, -1):
        d = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=k)).strftime(
            "%Y-%m-%d"
        )
        n_full = have_full.get(d, 0)
        n_lite = have_lite.get(d, 0)
        n = min(n_full, n_lite)
        if n == 0:
            gaps.append(d)
            np.log(f"  洞：{d}（两个文件都没有这天）")
        elif n < min_items:
            gaps.append(d)
            np.log(f"  洞：{d}（仅 {n} 条，< {min_items}，判定为残缺）")
    return gaps


def main():
    args = sys.argv[1:]
    days_back = DEFAULT_DAYS
    min_items = DEFAULT_MIN_ITEMS
    if "--days" in args:
        try:
            days_back = int(args[args.index("--days") + 1])
        except Exception:
            pass
    if "--min-items" in args:
        try:
            min_items = int(args[args.index("--min-items") + 1])
        except Exception:
            pass

    if not np.ZHIPU_API_KEY:
        np.log("缺少 ZHIPU_API_KEY，退出")
        return 2

    np.log(f"自愈检查：最近 {days_back} 天，条数下限 {min_items}")
    gaps = find_gaps(days_back, min_items)
    if not gaps:
        np.log("没有缺失日期，本次无需处理")
        return 0
    np.log(f"待补日期：{gaps}")

    np.log("抓取全部素材 ...")
    all_items = bf.collect_all()

    results = []
    for day in gaps:
        material, raw_n = bf.day_material(all_items, day)
        np.log(f"{day}: 当日原始 {raw_n} 条，可用素材 {len(material)} 条")
        if len(material) < 6:
            np.log(f"  素材不足 6 条（含邻域），跳过 {day}（下次再试）")
            continue
        summary, items = bf.gen_day(material, day)
        if not items:
            np.log(f"  生成失败（0 条），跳过 {day}")
            continue
        n_cn = sum(1 for x in items if x.get("region") == "cn")
        n_intl = len(items) - n_cn
        # 自愈阶段放宽一档：只要不是明显残废就写上去，
        # 宁可少一两条，也不要让线上整天空白。
        if n_cn < 4 or n_intl < 3:
            np.log(
                f"  产出过少（国内 {n_cn} / 国外 {n_intl}），跳过 {day}（下次再试）"
            )
            continue
        np.log(f"  ✓ {day} 生成 {len(items)} 条（国内 {n_cn} / 国外 {n_intl}）")
        weekday = np.WEEKDAYS[datetime.strptime(day, "%Y-%m-%d").weekday()]
        results.append((day, bf.js_block(day, weekday, summary, items), len(items)))
        time.sleep(1)

    if not results:
        np.log("本次没能补出任何日期（素材或模型状态不佳），下次再试")
        return 1

    results.sort(key=lambda x: x[0], reverse=True)
    days_sorted = [r[0] for r in results]
    blocks = [r[1] for r in results]
    for p in (FULL_HTML, LITE_HTML):
        bf.insert_blocks(p, blocks, days_sorted, replace=True)

    bad = False
    for p in (FULL_HTML, LITE_HTML):
        if np.check_js_syntax(p) is False:
            bad = True
            np.log(f"JS 校验失败: {p}")
    if bad:
        return 1
    np.log(f"自愈完成：补上 {len(results)} 天 → {days_sorted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
