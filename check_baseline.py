#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基线核查：逐日复测线上产业动态是否达到 8 月定版标准。

为什么要这个脚本
----------------
[2026-09-14] 用户诉求："看下 8 月份推送的产业动态，标题和内容都是咋写的，
以后都按照 8 月份的标准去推送。" 于是把 8 月（29 天 236 条）全量实测，
量化成下面这套基线。此后每次改提示词、换模型、调参数，都用它验收，
避免再出现"9 月正文均值掉到 22 字"却没人发现的情况。

8 月定版基线（2026-08-01 ~ 2026-08-29，236 条实测）
--------------------------------------------------
  条数/天      8 条（四类各 2 条）
  标题均值     25.0 字（中位 24，区间 11-55）
  正文均值     118.9 字（中位 114，区间 71-189）
  正文 <60 字  0 条
  100-160 字   164 条（69%）
  标题含数字   56%
  正文含数字   81%
  标题含逗号   38%（双分句结构）
  四类分布     capital 54 / industry 68 / policy 50 / tech 64

9 月退化对照（同一脚本实测）
  9-01 ~ 9-08  正文均 97.5-131.5（正常）
  9-09 ~ 9-14  正文均 22.4-36.0（崩盘）
  根因不是提示词，是 9-08 云端接管后模型由"WorkBuddy 智能体 + 联网搜索"
  换成免费 glm-4-flash；批量一次生成 10 条时模型会把每条压到 51 字左右。

用法
----
    python3 check_baseline.py                      # 抓线上完整版
    python3 check_baseline.py <本地html路径>
    python3 check_baseline.py <url>
    python3 check_baseline.py --since 2026-09-01   # 只看某日期之后

退出码：0=全部达标，1=存在未达标日期。
"""
import re
import subprocess
import sys
from collections import Counter

DEFAULT_URL = "https://bairunxuan123.github.io/ccid-ai-news/ai-chain-map.html"

# —— 8 月定版实测基线 ——
# 注意：这几个 MIN/MAX 是"报警线"，取 8 月真实区间的下限再留一点余量，
# 不是 8 月的平均值。判据口径见 judge() 的 docstring。
BASE_TITLE_AVG = 25.0
BASE_TITLE_MIN = 10      # 单条标题短于此值判异常（8 月最短 11 字）
BASE_TITLE_MAX = 40      # 生成端的上限（news_pipeline.MAX_TITLE_LEN），核查不判
BASE_DESC_AVG = 118.9
BASE_DESC_MIN = 70       # 单条正文短于此值判异常（8 月最短 71 字）
BASE_DESC_MAX = 300      # 生成端兜底上限，核查不判
TARGET_ITEMS = 8         # 用户硬要求：每天 8 条（只判下限）
CAT_LABELS = ("policy", "tech", "industry", "capital")


def load(src):
    if src.startswith("http"):
        out = subprocess.run(["curl", "-sL", "--max-time", "30", src],
                             capture_output=True)
        return out.stdout.decode("utf-8", "replace")
    with open(src, encoding="utf-8") as f:
        return f.read()


def parse(src):
    i = src.find("var NEWS_DATA = [")
    if i < 0:
        raise SystemExit("未找到 NEWS_DATA")
    j = src.find("\n];", i)
    body = src[i + len("var NEWS_DATA = ["):j]
    pat = re.compile(r'  \{\s*\n\s*date: "(\d{4}-\d{2}-\d{2})",')
    pos = [(m.start(), m.group(1)) for m in pat.finditer(body)]
    pos.append((len(body), None))
    rows = []
    for k in range(len(pos) - 1):
        s, d = pos[k]
        blk = body[s:pos[k + 1][0]]
        titles = re.findall(r'title: "([^"]*)"', blk)
        descs = re.findall(r'desc: "([^"]*)"', blk)
        cats = re.findall(r'cat: "([^"]*)"', blk)
        if not titles:
            continue
        tl = [len(t) for t in titles]
        dl = [len(x) for x in descs] or [0]
        rows.append(dict(
            date=d, n=len(titles),
            tavg=sum(tl) / len(tl), tmin=min(tl), tmax=max(tl),
            davg=sum(dl) / len(dl), dmin=min(dl), dmax=max(dl),
            dist=dict(Counter(cats)),
            # 双分句：8 月有 38% 的标题是"前半句，后半句"结构
            two_clause=sum(1 for t in titles if "，" in t) / len(titles),
            # 含数字：8 月标题 56% / 正文 81%
            t_num=sum(1 for t in titles if re.search(r"\d", t)) / len(titles),
            d_num=sum(1 for x in descs if re.search(r"\d", x)) / max(len(descs), 1),
        ))
    return rows


def judge(r):
    """返回问题列表（空 = 达标）。

    阈值取 8 月的**真实区间**，而不是我拍的整数线 —— 否则 8 月下旬
    "9 条新闻""标题 41 字"这类正常波动天天报警，脚本就没人看了。
    实测 8 月区间：标题 11-55（单日均值 18.9-35.9）、正文 71-189
    （单日均值 102.4-164.0），故：
      · 条数只判下限 8（用户硬要求），多于 8 条是好现象，不判
      · 标题最长不判（8 月本身就有 55 字的个案）
      · 正文最长不判（8 月有 189 字的长条目）
    """
    bad = []
    if r["n"] < TARGET_ITEMS:
        bad.append(f"仅{r['n']}条")
    if r["tavg"] < BASE_TITLE_AVG * 0.6:          # <15 字
        bad.append(f"标题均{r['tavg']:.0f}")
    if r["tmin"] < BASE_TITLE_MIN:                # <10 字
        bad.append(f"标题最短{r['tmin']}")
    if r["davg"] < BASE_DESC_AVG * 0.8:           # <95 字
        bad.append(f"正文均{r['davg']:.0f}")
    if r["dmin"] < BASE_DESC_MIN:                 # <70 字
        bad.append(f"正文最短{r['dmin']}")
    return bad


def main():
    args = [a for a in sys.argv[1:]]
    since = ""
    if "--since" in args:
        k = args.index("--since")
        since = args[k + 1]
        del args[k:k + 2]
    src = args[0] if args else DEFAULT_URL
    rows = parse(load(src))
    if since:
        rows = [r for r in rows if r["date"] >= since]
    print(f"数据源: {src}")
    print(f"共 {len(rows)} 天\n")
    hdr = (f"{'日期':<12}{'条数':>5}{'标题均':>8}{'标题区':>12}{'正文均':>8}"
           f"{'正文区':>12}{'双分句':>8}{'数字率':>8}  类别分布")
    print(hdr)
    print("-" * 108)
    bad = []
    for r in rows:
        issues = judge(r)
        if issues:
            bad.append((r["date"], issues))
        dist = " ".join(f"{k}{v}" for k, v in sorted(r["dist"].items()))
        mark = "OK" if not issues else "⚠ " + ",".join(issues)
        tspan = f"{r['tmin']}-{r['tmax']}"
        dspan = f"{r['dmin']}-{r['dmax']}"
        print(f"{r['date']:<12}{r['n']:>5}{r['tavg']:>8.1f}{tspan:>12}"
              f"{r['davg']:>8.1f}{dspan:>12}"
              f"{r['two_clause'] * 100:>7.0f}%{r['t_num'] * 100:>7.0f}%  {dist:<24}{mark}")
    n = len(rows) or 1
    print("-" * 108)
    print(f"标题均 {sum(r['tavg'] for r in rows) / n:.1f} 字（8月基线 {BASE_TITLE_AVG}）"
          f"｜正文均 {sum(r['davg'] for r in rows) / n:.1f} 字（8月基线 {BASE_DESC_AVG}）"
          f"｜双分句 {sum(r['two_clause'] for r in rows) / n * 100:.0f}%（8月 38%）")
    if bad:
        print(f"\n⚠ 未达标日期 {len(bad)} 天：")
        for d, iss in bad:
            print(f"    {d}  {'，'.join(iss)}")
        return 1
    print("\n🎉 全部日期均达到 8 月定版标准")
    return 0


if __name__ == "__main__":
    sys.exit(main())
