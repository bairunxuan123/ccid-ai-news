#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本 3：用真实 API 跑一遍改造后的两阶段生成，验证 8 月标准能否落地。

与 mock 的区别：走真实 glm-4-flash + 真实素材 + 真实过滤链，
但不写任何文件（不碰 HTML、不提交）。

输出：条数 / 标题字数分布 / 正文字数分布 / 四类分布 / 逐条正文原文，
以及【过滤漏斗】——阶段 A 拦了几条、阶段 B 修了几轮、最终丢弃几条及原因，
便于人工判断是否真的回到 8 月定版的写法。

[2026-09-14 迭代] 第一版只打印结果，看不到"为什么只剩 3 条"。
现在给 np.log 打桩，把全部丢弃/待修日志收进 _LOG，按原因归类统计后输出。

[2026-09-14 二次迭代] 上一轮真实复验（run 34819377914）暴露了两个 mock 抓不到的
P0：正文里抄进了提示词指令、四类缺了"产业动态"整类。这两类问题的定位信息
（哪一类候选不足、哪一类写不出合格正文、重写是因为哪个体检项）此前都不在输出里，
只能翻后台日志。现在补齐：
  · 阶段 A 每轮各类候选数（判断是不是候选阶段就缺类）
  · 阶段 B 各类合格正文数（判断是不是写不出/被筛掉）
  · 修复原因细分到 5 项（字数/数字/英文/混入指令/空泛表述）
  · 成品逐条的 leak / vague 复检（成品里一次都不许出现）
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
        ("标题混入提示词", "丢弃标题混入提示词"),
        ("聚合/消费电子", "丢弃聚合或消费电子类"),
        ("话题重复", "丢弃话题重复"),
        ("调用全失败", "丢弃调用全失败"),
        ("三轮仍不合格", "丢弃三轮重写仍不合格"),
        ("标题数字不可核实", "丢弃标题数字不可核实"),
    ]
    out = []
    for label, key in pats:
        n = sum(1 for m in _LOG if key in m)
        if n:
            out.append((label, n))
    rep = {
        "字数不足": sum(1 for m in _LOG if "正文待修" in m and "字数不足" in m),
        "数字对不上": sum(1 for m in _LOG if "正文待修" in m and "数字对不上" in m),
        "英文残留": sum(1 for m in _LOG if "正文待修" in m and "英文残留" in m),
        "混入指令": sum(1 for m in _LOG if "正文待修" in m and "混入指令" in m),
        "空泛表述": sum(1 for m in _LOG if "正文待修" in m and "空泛表述" in m),
    }
    return out, rep


def _stage_a_lines():
    """截取阶段 A 每轮的候选分布行，例如：
    '阶段 A 第1轮：候选 11 条 政策发布3 技术突破4 产业动态1 投融资3｜仍缺 产业动态'"""
    return [m for m in _LOG if "阶段 A 第" in m]


def _stage_b_lines():
    """截取阶段 B 各类合格正文数（由 news_pipeline 在收工时打印）"""
    return [m for m in _LOG if "阶段 B 合格正文" in m]


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

    drops, rep = _funnel()
    print("过滤漏斗（丢弃原因统计）：")
    if drops:
        for label, n in drops:
            print(f"  · {label}: {n} 条")
    else:
        print("  · 无丢弃")
    print("阶段 B 定向修复：" + " ｜ ".join(f"{k} {v} 次" for k, v in rep.items()))

    # 缺类定位：先看候选阶段（阶段 A）够不够，再看撰写阶段（阶段 B）有没有写出来。
    # 这两处的修法完全不同 —— 候选不足要改提示词/补选，写不出要改正文体检门槛。
    a_lines = _stage_a_lines()
    if a_lines:
        print("\n阶段 A 候选分布（每类不足 %d 条会触发补选）：" % np.MIN_CANDS_PER_CAT)
        for m in a_lines:
            print("    " + m.strip())
    b_lines = _stage_b_lines()
    if b_lines:
        print("阶段 B 合格正文分布（每类不足 %d 条说明该类被门槛筛掉了）："
              % np.MIN_PER_CAT_DESC)
        for m in b_lines:
            print("    " + m.strip())

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

    material_text = " ".join(
        (m.get("title", "") + " " + (m.get("summary") or "")) for m in mat)
    leaks, vagues, shorts = [], [], []
    for it in items:
        lk = np.prompt_leak(it["desc"])
        if lk:
            leaks.append((it["title"][:20], lk))
        vg = np.vague_phrases(it["desc"])
        if vg:
            vagues.append((it["title"][:20], vg))
        if len(it["desc"]) < np.MIN_DESC_LEN:
            shorts.append(it["title"][:20])

    for i, it in enumerate(items, 1):
        print(f"--- {i}. [{it['catLabel']}] {it['title']}（{len(it['title'])}字）")
        print(f"    来源 {it['source']} ｜ {it['url']}")
        print(f"    正文（{len(it['desc'])}字）：{it['desc']}\n")

    cat_counts = [dist.get(np.CAT_LABELS[k], 0) for k in np.CAT_LABELS]
    checks = [
        ("产出 8 条", len(items) == 8),
        ("标题均 ≥15 字且 ≤40 字", min(tl) >= 15 and max(tl) <= 40),
        ("正文均 ≥80 字", min(dl) >= 80),
        ("正文均值 ≥110 字（贴近 8 月 118.9）", sum(dl) / len(dl) >= 110),
        ("四类齐全", len(dist) == 4),
        ("四类各 ≥2 条（用户硬要求）", all(c >= 2 for c in cat_counts)),
        ("成品无提示词指令泄露", not leaks),
        ("成品无空泛评价", not vagues),
        ("成品无过短正文", not shorts),
    ]
    if leaks:
        print(f"⚠ 指令泄露：{leaks}")
    if vagues:
        print(f"⚠ 空泛评价：{vagues}")
    if shorts:
        print(f"⚠ 过短正文：{shorts}")
    print("=" * 74)
    for name, good in checks:
        print(f"  {'✅' if good else '❌'} {name}")
    print("=" * 74)
    ok = all(g for _, g in checks)
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
