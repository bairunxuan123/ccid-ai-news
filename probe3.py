#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本 3：用真实 API 跑一遍改造后的两阶段生成，验证双板块基线能否落地。

与 mock 的区别：走真实 glm-4-flash + 真实素材 + 真实过滤链，
但不写任何文件（不碰 HTML、不提交）。

输出：条数 / 标题字数分布 / 正文字数分布 / 板块×四类分布 / 逐条正文原文，
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

[2026-09-21 三次迭代] 双板块（国内 8 + 国外 8 = 16 条 / 八格各 2）：
  · 阶段 A 候选分布按 (region, cat) 八格统计
  · 收工条数目标 16，八格各 2 条作为硬门槛
  · 标题/正文质量阈值与 8 月基线一致（25 / 118.9）
  · 摘要按地域合成「【国内】... 【国外】...」
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
    """按原因归类过滤日志，还原"素材 → 落地 16 条"的漏斗。"""
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
        ("地域不足", "丢弃地域不足"),
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
    """截取阶段 A 每轮的候选分布行（双板块：'【国内】第1轮：候选 16 条 ...'）。"""
    return [m for m in _LOG if "阶段 A 第" in m or "阶段 A【" in m]


def _stage_b_lines():
    """截取阶段 B 各类合格正文数（由 news_pipeline 在收工时打印）"""
    return [m for m in _LOG if "阶段 B 合格正文" in m or "八格" in m]


def main():
    if not np.ZHIPU_API_KEY:
        print("缺少 ZHIPU_API_KEY")
        return 2
    np.log = _capture          # 打桩收日志
    day_cn = "2026年9月22日"
    print("=" * 74)
    print(f"诊断 3：真实 API 跑两阶段生成（{day_cn}，双板块目标 16 条）")
    print("=" * 74)

    mat = np.collect_material(hours=72)
    print(f"素材 {len(mat)} 条\n")
    if len(mat) < np.PER_REGION_ITEMS:    # 双板块起码要 12 条素材
        print(f"素材不足（<{np.PER_REGION_ITEMS}），终止")
        return 1

    t0 = time.time()
    summary, items = np.generate_news(mat, day_cn, attempt=0)
    dt = time.time() - t0

    print(f"\n{'=' * 74}")
    print(f"结果：{len(items)} 条 / 目标 {np.MAX_ITEMS}（国内 8 + 国外 8）"
          f" ｜ 耗时 {dt:.0f}s")
    print("=" * 74)

    drops, rep = _funnel()
    print("过滤漏斗（丢弃原因统计）：")
    if drops:
        for label, n in drops:
            print(f"  · {label}: {n} 条")
    else:
        print("  · 无丢弃")
    if any(rep.values()):
        print("阶段 B 定向修复：" + " ｜ ".join(f"{k} {v} 次" for k, v in rep.items() if v))

    # 缺类/缺板块定位：先看候选阶段（阶段 A）够不够，再看撰写阶段（阶段 B）有没有写出来。
    a_lines = _stage_a_lines()
    if a_lines:
        print("\n阶段 A 候选分布（按地域×四类，每格不足 %d 条会触发补选）："
              % np.MIN_CANDS_PER_CELL)
        for m in a_lines:
            print("    " + m.strip())
    b_lines = _stage_b_lines()
    if b_lines:
        print("阶段 B 合格正文分布（每格不足 %d 条说明该格被门槛筛掉）："
              % np.MIN_PER_CELL_DESC)
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
    # 双板块×四类分布
    cell_dist = {}
    for i in items:
        k = (i.get("region", "?"), i["cat"])
        cell_dist[k] = cell_dist.get(k, 0) + 1
    region_dist = {}
    for i in items:
        r = i.get("region", "?")
        region_dist[r] = region_dist.get(r, 0) + 1
    print(f"\n板块分布：{region_dist}（目标 国内 8 / 国外 8）")
    print("八格分布：")
    for region in np.REGIONS:
        cells = " ".join(
            f"{cat}{cell_dist.get((region, cat), 0)}"
            for cat in np.CAT_LABELS
        )
        print(f"  {np.REGION_LABELS[region]}: {cells}")
    print(f"\n标题字数 平均 {sum(tl)/len(tl):.1f}｜区间 {min(tl)}-{max(tl)}"
          f"（8月基准 {np.BASE_TITLE_AVG if hasattr(np, 'BASE_TITLE_AVG') else 25.0}）")
    print(f"正文字数 平均 {sum(dl)/len(dl):.1f}｜区间 {min(dl)}-{max(dl)}"
          f"（8月基准 118.9）")
    dual = sum(1 for i in items if "，" in i["title"])
    print(f"标题双分句 {dual}/{len(items)}（8月基准约 1/3）")
    print(f"摘要：{summary}\n")

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

    # 逐条正文展示（按板块分组）
    for region in np.REGIONS:
        region_items = [i for i in items if i.get("region") == region]
        if not region_items:
            continue
        print(f"\n{'─' * 30} {np.REGION_LABELS[region]}板块"
              f"（{len(region_items)} 条） {'─' * 30}")
        for k, it in enumerate(region_items, 1):
            print(f"  {k}. [{it['catLabel']}] {it['title']}（{len(it['title'])}字）")
            print(f"      来源 {it['source']} ｜ {it['url']}")
            print(f"      正文（{len(it['desc'])}字）：{it['desc']}\n")

    # 八格检查
    eight_cells = [(r, c) for r in np.REGIONS for c in np.CAT_LABELS]
    cell_ok = all(cell_dist.get(k, 0) >= np.MIN_PER_CELL_DESC for k in eight_cells)

    checks = [
        (f"产出 {np.MAX_ITEMS} 条（双板块满载）", len(items) == np.MAX_ITEMS),
        ("国内 8 条 + 国外 8 条",
         region_dist.get("cn", 0) == np.PER_REGION_ITEMS
         and region_dist.get("intl", 0) == np.PER_REGION_ITEMS),
        ("八格各 ≥2 条", cell_ok),
        ("标题均 ≥15 字", min(tl) >= 15),
        ("标题均 ≤40 字", max(tl) <= 40),
        ("正文最短 ≥80 字", min(dl) >= 80),
        ("正文均值 ≥110 字（贴近 8 月 118.9）", sum(dl) / len(dl) >= 110),
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

    # 完整链路日志【无条件打印】。
    # [2026-09-14] 上一版只在"一条都没产出"时才打印，结果 run 34821688274 产出 6 条
    # 却四类不均衡（技术突破/产业动态/投融资各 1 条），而"为什么另外 9 条候选被丢掉"
    # 的关键信息全在被吞掉的日志里 —— 只能重跑一次真实 API（约 6 分钟、40+ 次调用）。
    # 诊断脚本的价值就在于一次跑完能定位，所以这里永远打印。
    print("\n—— 完整链路日志（阶段 A 候选 / 阶段 B 逐条体检与丢弃原因）——")
    for m in _LOG:
        print("  " + m)

    ok = all(g for _, g in checks)
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())