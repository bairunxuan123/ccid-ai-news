#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""news_pipeline 回归测试：数字溯源（numbers_grounded）口径。

背景：8 月定版风格要求正文带具体数字（金额/数量/占比/倍数），
但系统必须拦住模型编造的数字。这两者天然冲突，口径稍有偏差就会：

  1. 漏放 —— 编造的数字被写进线上（严重）
  2. 误杀 —— 忠实于素材的真实数字被判为编造，整条丢弃（曾导致"投融资常年 0 条"）

本文件锁定第 2 类误杀的四类典型成因，防止后续改动回退。

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
    print(f"结果：{ok}/{ok + fail} 通过" + ("　🎉 全部通过" if fail == 0 else f"　⚠ {fail} 条失败"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
