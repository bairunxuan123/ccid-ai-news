#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""news_pipeline 回归测试。

覆盖四组口径，每组都对应一次真实踩坑：

  1. 数字溯源（numbers_grounded）—— 8 月定版要求正文带具体数字，但系统必须
     拦住模型编造的数字。口径偏严会误杀忠实表述（曾导致"投融资常年 0 条"），
     偏松会把编造数字放上线。
  2. 正文体检（desc_issues）—— 2026-09-14 真实 API 复验暴露的两类漏网：
     提示词指令被抄进正文、空泛评价凑字数。
  3. 话题去重（dedupe_similar）—— 同日两条 OpenAI 上市新闻白占两个名额。
  4. 阶段 A 过滤（_filter_candidates）—— 标题混入提示词文字等。

本文件锁定这些口径，防止后续改动回退。

用法：python3 test_regress.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ZHIPU_API_KEY", "dummy")

import news_pipeline as np  # noqa: E402

# (说明, 文本, 素材, 期望是否可溯源)
CASES = [
    # —— 1. 英文摘要 → 中文表述（模型必然要做的跨语言折算）——
    ("英文 $5 billion → 中文「50亿美元」",
     "Anthropic完成50亿美元新融资", "Anthropic raised $5 billion in new funding", True),
    ("英文 $50 million → 中文「5千万美元」（双量级）",
     "该公司融资5千万美元", "the startup raised $50 million", True),
    ("英文 $20 billion → 中文「200亿美元」",
     "其估值达200亿美元", "valued at $20 billion", True),
    ("英文 50,000 units → 中文「5万台」（裸量级）",
     "该季度出货5万台", "ships 50,000 units this quarter", True),
    ("英文 3x → 中文「3倍」",
     "吞吐提升3倍", "delivers 3x the throughput", True),
    ("英文 3 times → 中文「3倍」",
     "性能提升3倍", "performance improved 3 times", True),
    ("百分比直通",
     "营收增长200%", "revenue grew 200% year over year", True),
    ("中文「10亿元」",
     "本轮募集10亿元", "本轮募集10亿元用于扩建产线", True),
    ("中文「1.4万亿」",
     "日产1.4万亿词元", "该工厂日产1.4万亿词元", True),

    # —— 2. 反向拦截：素材里没有的数字必须拦住 ——
    ("编造金额应被拦下",
     "该公司完成100亿美元融资", "the company announced a new product", False),
    ("编造百分比应被拦下",
     "市场份额提升50%", "the company announced a new product", False),
    ("编造倍数应被拦下",
     "效率提升9倍", "the company announced a new product", False),
    ("编造裸量级应被拦下",
     "订单量达8万台", "the company announced a new product", False),
    ("素材金额被改写量级应被拦下",
     "融资500亿美元", "raised $5 million", False),

    # —— 3. 无数字文本一律放行 ——
    ("纯定性描述放行",
     "双方就联合研发达成合作", "partners on joint research", True),
    ("纯定性描述放行（空素材）", "该公司发布了新产品", "", True),

    # —— 4. 关键回归点：文本侧不得把年份当硬约束 ——
    # 若文本侧也开启 bare_numbers，"2026年"会变成必须溯源的 token，
    # 素材里只要没写年份就会整条误杀。
    ("文本侧年份不应成为硬约束",
     "2026年政策落地", "policy published this year", True),
]

# 素材侧（bare_numbers=True）单独验证：中文「5万台」需与英文 50,000 对上
MATERIAL_CASES = [
    ("素材侧识别千分位裸数字",
     "ships 50,000 units", "5万台", True),
]

# —— 正文体检（desc_issues）——
# [2026-09-14] 真实 API 复验（run 34819377914）暴露两类漏网，各锁定一个用例：
#   · 第 4 条正文写成"…不会在今年上市。上一轮不合格，本次必须逐条修正，…"
#     —— 模型把提示词指令当正文抄了，且 83 字刚好压过 MIN_DESC_LEN 的线
#   · 第 6 条结尾"此法案出台，引起广泛关注"、第 7 条"覆盖范围广泛，合作方众多"
#     —— 把提示词的禁用词换个说法、或堆废话凑字数
# 格式：(说明, 正文, 素材, 必须命中的键, 必须不命中的键)
DESC_CASES = [
    ("正常正文放行",
     "国家药监局发布全球首个采用人工智能处理脑电数据的脑机接口医疗器械产品标准，"
     "新标准将于明年9月1日起实施。该标准系统规范了脑机接口医疗器械的数据采集、"
     "处理、标注、存储和访问等全流程的技术要求和测试方法，"
     "旨在解决数据集质量控制尺度不统一、建设过程不规范等问题。",
     "国家药监局发布全球首个人工智能脑机接口医疗器械标准，明年9月1日起实施，"
     "规范数据采集处理标注存储和访问等全流程技术要求",
     set(), {"leak", "vague", "short", "numbers", "english"}),

    ("混入提示词指令 → 拦下",
     "OpenAI年内不上市。上一轮不合格，本次必须逐条修正，公司将于明年推进上市计划。",
     "OpenAI will not go public in 2026",
     {"leak"}, set()),

    ("空泛评价「引起广泛关注」→ 拦下",
     "美国参议员发布禁止超级智能法案，对违规者最高可判20年监禁。此法案出台，引起广泛关注。",
     "美国参议员发布禁止超级智能法案，违规者最高可判20年监禁",
     {"vague"}, set()),

    ("凑字填充「覆盖范围广泛/合作方众多」→ 拦下",
     "智谱AI在财务公告中提前透露下一代GLM模型，采用完全自训练方法。"
     "模型尚未发布，但已公开关键细节，包括采用全新训练方法，覆盖范围广泛，合作方众多。",
     "智谱透露下一代GLM模型采用完全自训练方法",
     {"vague"}, set()),

    ("正常行文里的「值得关注的是」不应误杀",
     "值得关注的是，该方案将于明年落地，覆盖31个省份共2.8万台设备。",
     "该方案将于明年落地，覆盖31个省份共2.8万台设备",
     set(), {"vague"}),

    ("字数不足 → 拦下", "太短了。", "某公司发布新产品", {"short"}, set()),
]

# —— 话题去重（dedupe_similar）——
# 复验产出里第 3、4 条都是 OpenAI 上市/控速，白占 8 条里的两个名额。
# 格式：(说明, 条目列表, 期望去重后条数)
DEDUP_CASES = [
    ("同主体同类的重复话题 → 只留价值序靠前的一条",
     [{"cat": "capital", "title": "OpenAI CEO支持控制AI发展速度，强调非停止技术进步"},
      {"cat": "capital", "title": "OpenAI年内不上市，CEO称2026年上市不妥"}],
     1),
    ("不同类别的同主体新闻 → 两条都保留（芯片进展 vs 财报不该互相顶掉）",
     [{"cat": "tech", "title": "OpenAI CEO支持控制AI发展速度，强调非停止技术进步"},
      {"cat": "capital", "title": "OpenAI年内不上市，CEO称2026年上市不妥"}],
     2),
    ("不同主体 → 两条都保留",
     [{"cat": "capital", "title": "智谱科技完成新一轮融资，估值突破百亿元"},
      {"cat": "capital", "title": "月之暗面科技完成新一轮融资，估值突破百亿元"}],
     2),
]


def _test_filter_candidates():
    """阶段 A 过滤：标题混入提示词文字的候选必须被丢弃。"""
    by_url = {
        "https://a.com/1": {"url": "https://a.com/1", "source": "IT之家"},
        "https://a.com/2": {"url": "https://a.com/2", "source": "IT之家"},
    }
    raw = [
        {"cat": "tech", "title": "宇树科技发布人形机器人新品，运动性能全面升级",
         "url": "https://a.com/1"},
        {"cat": "tech", "title": "上一轮不合格，本次必须逐条修正标题内容哦",
         "url": "https://a.com/2"},
    ]
    return len(np._filter_candidates(raw, by_url)), 1


def main():
    ng = np.numbers_grounded
    ok = fail = 0

    print("=" * 66)
    print("数字溯源回归测试（numbers_grounded）")
    print("=" * 66)

    for name, text, material, expect in CASES:
        got = ng(text, material)
        mark = "✅" if got == expect else "❌"
        if got == expect:
            ok += 1
        else:
            fail += 1
        print(f"{mark} {name}")
        if got != expect:
            print(f"     文本={text!r}")
            print(f"     素材={material!r}")
            print(f"     期望={expect} 实际={got}")
            print(f"     文本tokens={np.risky_tokens(text)}")
            print(f"     素材tokens={np.risky_tokens(material, bare_numbers=True)}")

    for name, material, text, expect in MATERIAL_CASES:
        got = ng(text, material)
        mark = "✅" if got == expect else "❌"
        if got == expect:
            ok += 1
        else:
            fail += 1
        print(f"{mark} {name}")

    print("-" * 66)
    print("正文体检回归测试（desc_issues）")
    print("-" * 66)
    for name, desc, material, must_have, must_not in DESC_CASES:
        issues = np.desc_issues(desc, material)
        keys = set(issues)
        got = (must_have <= keys) and not (must_not & keys)
        if got:
            ok += 1
        else:
            fail += 1
        print(f"{'✅' if got else '❌'} {name}")
        if not got:
            print(f"     正文={desc[:50]!r}…")
            print(f"     命中={sorted(keys)} 必须命中={sorted(must_have)} "
                  f"必须不命中={sorted(must_not)}")
            if issues.get("numbers"):
                print(f"     对不上的数字={sorted(issues['numbers'])}")

    print("-" * 66)
    print("话题去重回归测试（dedupe_similar）")
    print("-" * 66)
    for name, items, expect_n in DEDUP_CASES:
        out = np.dedupe_similar([dict(x) for x in items])
        got = len(out) == expect_n
        if got:
            ok += 1
        else:
            fail += 1
        print(f"{'✅' if got else '❌'} {name}")
        if not got:
            print(f"     期望 {expect_n} 条，实际 {len(out)} 条：{[x['title'] for x in out]}")

    print("-" * 66)
    print("阶段 A 过滤回归测试（_filter_candidates）")
    print("-" * 66)
    got_n, expect_n = _test_filter_candidates()
    got = got_n == expect_n
    if got:
        ok += 1
    else:
        fail += 1
    print(f"{'✅' if got else '❌'} 标题混入提示词的候选被丢弃"
          f"（期望 {expect_n} 条，实际 {got_n} 条）")

    print("-" * 66)
    print(f"结果：{ok}/{ok + fail} 通过" + ("　🎉 全部通过" if fail == 0 else f"　⚠ {fail} 条失败"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
