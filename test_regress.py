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
    # —— 必须去掉：同一事件、措辞近乎逐字重复（重合度 0.82-0.83）——
    ("同一事件措辞几乎一致 → 只留价值序靠前的一条",
     [{"cat": "capital", "title": "特斯拉Robotaxi将搭载FSD V15，下月全天候运营"},
      {"cat": "capital", "title": "特斯拉Robotaxi下月全天候运营，搭载FSD V15"}],
     1),
    ("同一事件仅书名号/连接词差异 → 只留一条",
     [{"cat": "policy", "title": "商务部等8部门印发促进智能家居消费行动方案"},
      {"cat": "policy", "title": "商务部等8部门印发《促进智能家居消费行动方案》"}],
     1),
    # —— 必须保留：同主体但确是两条不同新闻（重合度 0.17-0.22）——
    # [2026-09-14 阈值校准] 这两组是关键反例：它们的"共同词片"都是 7，
    # 与上面 OpenAI 那类的共同词片相同，用词片数根本分不开；Jaccard 才分得开。
    # 原阈值（共同词片 ≥5）会把它们误杀，真实复验里因此丢过条目。
    ("同一公司不同事件 → 两条都保留（发布新品 vs 科创板挂牌）",
     [{"cat": "industry", "title": "宇树科技发布人形机器人G1+，全面升级运动性能与感知交互"},
      {"cat": "industry", "title": "宇树科技科创板挂牌，人形机器人第一股诞生"}],
     2),
    ("同一公司不同角度 → 两条都保留（上市时机 vs 控速表态）",
     [{"cat": "capital", "title": "OpenAI CEO支持控制AI发展速度，强调非停止技术进步"},
      {"cat": "capital", "title": "OpenAI年内不上市，CEO称2026年上市不妥"}],
     2),
    ("不同类别的同主体新闻 → 两条都保留（技术进展 vs 上市）",
     [{"cat": "tech", "title": "宇树科技发布人形机器人G1+，运动性能全面升级"},
      {"cat": "capital", "title": "宇树科技科创板挂牌，人形机器人第一股诞生"}],
     2),
    ("不同主体 → 两条都保留",
     [{"cat": "capital", "title": "智谱科技完成新一轮融资，估值突破百亿元"},
      {"cat": "capital", "title": "月之暗面科技完成新一轮融资，估值突破百亿元"}],
     2),
]


# —— 「政策发布」的国内官方口径（is_cn_official_policy / normalize_category）——
# [2026-09-18 用户要求] "政策发布尽量用国内官方政府发的人工智能相关政策，
#   不要随便一条动态都叫政策发布"。此前 POLICY_HINTS 里的"监管/标准/法案/提案"
#   是泛词，任何带"监管"的稿子（含外国议员提案）都能算政策发布。
# 格式：(说明, 模型标的类目, 标题, 正文/摘要, 期望类目)
CAT_CASES = [
    # —— 应判为政策发布：国内官方主体 + 政策动作 + AI 主题 ——
    ("工信部印发AI行动方案 → policy（模型标对了，保持）", "policy",
     "工业和信息化部关于印发《“人工智能+软件”专项行动实施方案》的通知",
     "工信部信发〔2026〕209号，现将方案印发给你们", "policy"),
    ("发改委发布算力行动计划，模型误标 tech → 改判 policy", "tech",
     "国家发展改革委发布算力基础设施高质量发展行动计划",
     "发改委印发通知，部署全国一体化算力网建设", "policy"),
    ("工信部印发指引，模型误标 industry → 改判 policy", "industry",
     "工信部印发《智能制造典型场景参考指引》，覆盖50个场景",
     "工业和信息化部办公厅发布通知", "policy"),
    ("地方政府发文也算官方政策", "policy",
     "北京市人民政府办公厅印发人工智能产业创新发展三年行动计划",
     "京政办发〔2026〕12号，聚焦大模型与智能体", "policy"),
    # —— 应改判：外国的法案/监管不算政策发布（本次收紧的重点）——
    ("美国参议员提法案 → 不是政策发布，改判 industry", "policy",
     "美国参议员提出禁止超级智能法案，违规者最高监禁20年",
     "美国国会正在讨论该提案", "industry"),
    ("欧盟AI法案实施细则 → 不是政策发布，改判 industry", "policy",
     "欧盟AI法案实施细则公布，55个高风险系统纳入监管",
     "欧盟委员会发布实施细则", "industry"),
    ("英国监管机构表态 → 不是政策发布", "policy",
     "英国监管机构就AI安全评估框架征求意见",
     "英国相关机构发布征求意见稿", "industry"),
    # —— 应改判：只出现"监管/合规/标准"这类泛词的新闻不算政策 ——
    ("企业被监管处罚 → 不是政策发布", "policy",
     "某科技公司因数据合规问题被监管部门罚款5000万元",
     "监管部门对其作出处罚决定", "industry"),
    ("只讲行业标准制定、无官方主体的新闻 → 不是政策发布", "policy",
     "业界呼吁建立大模型行业标准，多家企业参与讨论",
     "相关标准仍在讨论阶段", "industry"),
    # —— 无 AI 主题的官方文件不算政策发布 ——
    ("官方文件但与AI无关 → 不是政策发布", "policy",
     "国务院办公厅关于进一步加强烟花爆竹全链条安全监管的意见",
     "国务院办公厅印发意见，部署安全监管工作", "industry"),
    # —— 资本类不能被政策判据吃掉 ——
    ("外国公司融资 → 仍为 capital", "capital",
     "Anthropic完成130亿美元新融资，估值达1830亿美元",
     "由多家机构领投", "capital"),
]


def _test_filter_candidates():
    """阶段 A 过滤：标题混入提示词的候选必须被丢弃。"""
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


def _check_source_numbers():
    """数字修复提示必须把"本条素材里可用的数字"摊给模型。

    [2026-09-14] 真实复验 run 34821688274 里 10 条正文因"数字对不上"回炉，
    修复提示只说"素材里没有这些数字"，模型只能再猜一次，三轮耗尽后整条丢弃。
    这两条断言分别锁住"列得出可用数字"和"信息不足时不再逼着凑长度"。
    """
    m = {"title": "某创企完成B轮融资", "source": "量子位",
         "summary": "该创企完成B轮融资，募集8亿元，投后估值60亿元，资金用于产线建设。"}
    got = np._source_numbers(m)
    r1 = ("8亿元" in got and "60亿元" in got)

    # 修复轮里必须出现"可用数字只有…"，否则模型无从下手
    p = np.build_desc_prompt(m, attempt=1, fix={"numbers": {"amt:12000000000"}})
    r2 = ("可以放心使用" in p and "60亿元" in p)

    # 字数不足的修复提示不得再逼"必须补到 110 字"，否则模型会编数字凑长度
    p2 = np.build_desc_prompt(m, attempt=1, fix={"short": True})
    r3 = ("绝对不要靠编造数字来凑长度" in p2 and "90-110 字即可" in p2)

    # 素材完全没有数字时，必须明说"不要写数字"
    m2 = {"title": "某机构发布治理指引", "source": "IT之家",
          "summary": "该机构发布人工智能治理指引，强调合规与安全。"}
    p3 = np.build_desc_prompt(m2, attempt=1, fix={"numbers": {"pct:40"}})
    r4 = ("没有任何可用数字" in p3)

    return [
        ("可用数字能列出来（8亿元、60亿元）", r1),
        ("修复提示摊开可用数字而不是只说'不对'", r2),
        ("字数修复不再逼着凑长度、并禁止编数字凑数", r3),
        ("素材无数字时明说'不要写数字'", r4),
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
    print("数字修复提示回归测试（_source_numbers / build_desc_prompt）")
    print("-" * 66)
    for name, good in _check_source_numbers():
        if good:
            ok += 1
        else:
            fail += 1
        print(f"{'✅' if good else '❌'} {name}")

    print("-" * 66)
    print("「政策发布」国内官方口径回归测试（is_cn_official_policy）")
    print("-" * 66)
    for name, cat, title, desc, want in CAT_CASES:
        got_cat = np.normalize_category(cat, title, desc)
        got = got_cat == want
        if got:
            ok += 1
        else:
            fail += 1
        print(f"{'✅' if got else '❌'} {name}")
        if not got:
            print(f"     模型标={cat} 期望={want} 实际={got_cat}"
                  f"｜is_cn_official_policy={np.is_cn_official_policy(title, desc)}")
            print(f"     标题={title[:46]}")

    print("-" * 66)
    print("地域归属回归测试（region_of / normalize_region，2026-09-21 双板块要求）")
    print("-" * 66)
    for name, title, desc, source, url, want in REGION_CASES:
        got = np.region_of(title, desc, source, url)
        good = got == want
        if good:
            ok += 1
        else:
            fail += 1
        print(f"{'✅' if good else '❌'} {name}")
        if not good:
            print(f"     期望={want} 实际={got}｜标题={title[:46]}")

    print("-" * 66)
    print("「国外板块」政策口径回归测试（is_intl_official_policy）")
    print("-" * 66)
    for name, cat, title, desc, region, want in INTL_POLICY_CASES:
        got_cat = np.normalize_category(cat, title, desc, region)
        good = got_cat == want
        if good:
            ok += 1
        else:
            fail += 1
        print(f"{'✅' if good else '❌'} {name}")
        if not good:
            print(f"     模型标={cat} 期望={want} 实际={got_cat}｜标题={title[:46]}")

    print("-" * 66)
    print(f"结果：{ok}/{ok + fail} 通过" + ("　🎉 全部通过" if fail == 0 else f"　⚠ {fail} 条失败"))
    return 1 if fail else 0



# —— 地域归属（region_of）——
# 页面拆成国内/国外两个板块后，每条新闻必须有地域标签；判定看主体而非媒体来源。
REGION_CASES = [
    ("部委发文→国内", "工信部印发《“人工智能+软件”专项行动实施方案》", "", "中国政府网",
     "https://www.gov.cn/zhengce/xx.htm", "cn"),
    ("三大运营商→国内", "AI-eSIM团体标准编制启动，三大运营商参与", "", "IT之家", "", "cn"),
    ("商汤→国内", "商汤大装置临港AIDC获“算效+算电”双5A认证", "", "量子位", "", "cn"),
    ("中文标题无主体→国内兜底", "一年连融三轮，金融AI公司拿下超3亿B轮", "", "量子位", "", "cn"),
    ("广东省政府→国内", "广东省人民政府发布人工智能赋能制造业行动方案", "", "南方日报", "", "cn"),
    ("微软→国外", "微软发布AI行为准则，禁止模型黑客系统或欺骗人类", "", "TechCrunch", "", "intl"),
    ("OpenAI→国外", "OpenAI联合创始人：人类已进入通用人工智能时代", "", "IT之家", "", "intl"),
    ("欧盟委员会→国外", "欧盟委员会发布AI法案实施细则，2026年起分阶段适用", "", "The Verge", "", "intl"),
    ("美国商务部（含“商务部”）→国外",
     "美国商务部将12家中国AI芯片企业列入实体清单", "", "路透社", "", "intl"),
    ("中国商务部（含“美国”）→国内",
     "中国商务部就美国AI芯片出口管制发表谈话", "", "IT之家", "", "cn"),
    ("Nvidia→国外", "Nvidia发布新一代GB300，训练性能提升3倍", "", "VentureBeat", "", "intl"),
    ("台积电→国内（中国台湾）", "台积电宣布扩产AI封装产能，月产提升3万片", "", "IT之家", "", "cn"),
    ("白宫→国外", "白宫发布AI行动计划，要求联邦机构加速采购", "", "The Verge", "", "intl"),
]

# —— 「国外板块」的政策口径（is_intl_official_policy）——
# 国外板块也有"政策发布"维度，但判据换成境外政府/监管机构的正式规则；
# 企业合规声明、行业倡议、议员个人提案仍然不算。
INTL_POLICY_CASES = [
    ("欧盟委员会正式通过条例→policy", "policy",
     "欧盟委员会通过人工智能法案实施条例，分阶段生效", "条例明确通用目的模型义务", "intl", "policy"),
    ("白宫发布AI行动计划→policy", "policy",
     "白宫发布人工智能行动计划，要求联邦机构加速采购", "计划要求扩大联邦算力", "intl", "policy"),
    ("企业合规声明→不算政策", "policy",
     "微软发布AI行为准则，禁止模型黑客系统或欺骗人类", "准则明确了模型应遵循的原则", "intl", "industry"),
    ("议员个人提案→不算政策", "policy",
     "美国参议员提出人工智能框架提案，拟设安全评估", "提案尚待国会审议", "intl", "industry"),
    ("国内板块下欧盟政策→不算政策（归产业）", "policy",
     "欧盟委员会通过人工智能法案实施条例，分阶段生效", "条例明确义务清单", "cn", "industry"),
]

if __name__ == "__main__":
    sys.exit(main())
