#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本 3：用真实 API 跑一遍改造后的两阶段生成，验证 8 月标准能否落地。

与 mock 的区别：走真实 glm-4-flash + 真实素材 + 真实过滤链，
但不写任何文件（不碰 HTML、不提交）。

输出：条数 / 标题字数分布 / 正文字数分布 / 四类分布 / 逐条正文原文，
以及【过滤漏斗】——阶段 A 拦了几条、阶段 B 修了几轮、最终丢弃几条及原因，
便于人工判断是否真的回到 8 月定版的写法。

[2026-09-14 迭代] 上一版只打印结果，看不到"为什么只剩 3 条"。
现在给 np.log 打桩，把全部丢弃/待修日志收进 _LOG，按原因归类统计后输出。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_pipeline as np  # noqa: E402

_LOG = []


def _capture(msg):
    _LOG.append(str(msg))


def _funnel():
    """按原因归类过滤日志，还原"素材 → 落地 8 条"的漏斗。"""
    pats = [
        ("编造 URL", "丢弃编造 URL"),
        ("重复素材", "丢弃重复素材"),
        ("标题过短", "丢弃标题过短"),
        ("标题过长", "丢弃标题过长"),
        ("标题未翻译", "丢弃标题未翻译"),
        ("聚合/消费电子", "丢弃聚合或消费电子类"),
        ("调用全失败", "丢弃调用全失败"),
        ("三轮仍不合格", "丢弃三轮重写仍不合格"),
        ("标题数字不可核实", "丢弃标题数字不可核实"),
    ]
    out = []
    for label, key in pats:
        n = sum(1 for m in _LOG if key in m)
        if n:
            out.append((label, n))
    rep_short = sum(1 for m in _LOG if "过短" in m and "正文待修" in m)
    rep_num = sum(1 for m in _LOG if "数字对不上" in m and "正文待修" in m)
    rep_en = sum(1 for m in _LOG if "英文残留" in m and "正文待修" in m)
    return out, rep_short, rep_num, rep_en


def main():
    if not np.ZHIPU_API_KEY:
        print("缺少 ZHIPU_API_KEY")
        return 2
    np.log = _capture          # 打桩收日志
    day_cn = "2026年9月14日"
    print("=" * 74)
    print(f"诊断 3：真实 API 跑两阶段生成（{day_cn}）")
    print("=" * 74)

    mat = np.collect_material(hours=72)
    print(f"素材 {len(mat)} 条\n")
    if len(mat) < 8:
        print("素材不足，终止")
        return 1

    t0 = time.time()
    summary, items = np.generate_news(mat, day_cn, attempt=0)
    dt = time.time() - t0

    print(f"\n{'=' * 74}")
    print(f"结果：{len(items)} 条 / 目标 8 ｜ 耗时 {dt:.0f}s")
    print("=" * 74)

    drops, rep_short, rep_num, rep_en = _funnel()
    print("过滤漏斗（丢弃原因统计）：")
    if drops:
        for label, n in drops:
            print(f"  · {label}: {n} 条")
    else:
        print("  · 无丢弃")
    print(f"阶段 B 定向修复：字数 {rep_short} 次 ｜ 数字 {rep_num} 次 ｜ 英文 {rep_en} 次")

    if not items:
        print("\n⚠ 未产出任何条目（两阶段链路需要排查）")
        print("\n—— 完整日志 ——")
        for m in _LOG:
            print("  " + m)
        return 1

    tl = [len(i["title"]) for i in items]
    dl = [len(i["desc"]) for i in items]
    dist = {}
    for i in items:
        dist[i["catLabel"]] = dist.get(i["catLabel"], 0) + 1
    print(f"\n标题字数 平均 {sum(tl)/len(tl):.1f}｜区间 {min(tl)}-{max(tl)}（8月基准 25.0）")
    print(f"正文字数 平均 {sum(dl)/len(dl):.1f}｜区间 {min(dl)}-{max(dl)}（8月基准 118.9）")
    dual = sum(1 for i in items if "，" in i["title"])
    print(f"标题双分句 {dual}/{len(items)}（8月基准约 1/3）")
    print(f"四类分布 {dist}")
    print(f"摘要：{summary}\n")

    for i, it in enumerate(items, 1):
        print(f"--- {i}. [{it['catLabel']}] {it['title']}（{len(it['title'])}字）")
        print(f"    来源 {it['source']} ｜ {it['url']}")
        print(f"    正文（{len(it['desc'])}字）：{it['desc']}\n")

    checks = [
        ("产出 8 条", len(items) == 8),
        ("标题均 ≥15 字且 ≤40 字", min(tl) >= 15 and max(tl) <= 40),
        ("正文均 ≥80 字", min(dl) >= 80),
        ("正文均值 ≥110 字（贴近 8 月 118.9）", sum(dl) / len(dl) >= 110),
        ("四类齐全", len(dist) == 4),
    ]
    print("=" * 74)
    for name, good in checks:
        print(f"  {'✅' if good else '❌'} {name}")
    print("=" * 74)
    return 0 if all(g for _, g in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
