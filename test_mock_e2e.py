#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端 mock 演练（不打真实 API）：验证两阶段生成链与 8 月标准的过滤/重试路径。

链路（2026-09-21 升级为"国内 / 国外双板块"）：
  素材池先按 region_of() 劈成国内 / 国外两份（split_material_by_region），
  阶段 A 按地域各跑一轮选题（各 16 条候选，四类 × 4）；
  阶段 B 逐条素材单独写正文，轮转键是 (region, cat)，
  收工条件是"8 个格子各满 MIN_PER_CELL_DESC 条"，即国内 8 + 国外 8 = 16 条。

六个场景（都模拟真实会发生的失败）：
  [主场景]   阶段 B 首次写得太短 → 按"字数不足"提示重写 → 达标 → 16 条
  [救援场景] 阶段 B 首次写了摘要里没有的数字 → 按"数字对不上"提示重写 → 救回
  [抄指令]   阶段 B 把修改要求原文抄进正文（长度/数字都合格）→ 按"混入了指令"重写
  [空泛评价] 阶段 B 结尾堆"引起广泛关注"这类废话 → 按"空泛表述"提示重写
  [补选场景] 阶段 A 国内池的 industry 候选只有 1 条 → 自动补选一轮 → 8 格齐备
  [外环]     阶段 B 三次都太短（模型完全不改）→ 0 条 → 触发选题重试（attempt=1）

断言重点：
  A. 阶段 A 的短标题/聚合类/编造 URL/英文原句候选被拦下，合格候选才进阶段 B
  B. 阶段 B 单条重写时，prompt 必须含针对性修正提示（否则重写是空转）
  C. 数字对不上时回灌的是【具体数字名】的修复提示，而不是笼统"重写"
  D. 外环重试 prompt 必须含"上一轮不合格，本次务必修正"提示
  E. 最终 16 条：国内 8 + 国外 8，且两边四类各 2 条（用户硬要求）
  F. 提示词指令、空泛评价一律不得留在成品里（复验 run 34819377914 的真实教训）
  G. 每条成品都带 region 字段，且国内池的候选不会跑到国外板块去
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("ZHIPU_API_KEY", "dummy")

import news_pipeline as np  # noqa: E402


# ===========================================================================
# 素材池：国内 / 国外 各 12 条（数字都可溯源），另加几条"应当被拦"的坏素材
# ===========================================================================
CN_MATERIAL = [
    {"title": "两部门联合发布AI计量体系指引", "url": "https://www.ithome.com/a1", "source": "IT之家",
     "summary": "市场监管总局与国家发改委联合印发《人工智能计量体系和能力建设指引（2026版）》，围绕六大板块系统布局，提出到2027年建成国家级计量技术研发应用中心。"},
    {"title": "工信部印发算力互联行动计划", "url": "https://www.ithome.com/a3", "source": "IT之家",
     "summary": "工业和信息化部印发智能算力互联互通行动计划，提出到2027年建成50个枢纽节点，跨区域算力调度时延下降30%，并统一算力标识与计量口径。"},
    {"title": "国家数据局印发数据要素三年行动方案", "url": "https://www.gov.cn/zhengce/content_e2.htm",
     "source": "中国政府网",
     "summary": "国家数据局印发《数据要素×三年行动方案》，覆盖12个重点行业，提出到2027年数据产业规模年均增速超过15%，支持人工智能训练数据合规供给。"},
    {"title": "寒武纪发布新一代云端推理芯片", "url": "https://www.qbitai.com/b1", "source": "量子位",
     "summary": "该芯片面向数据中心推理场景，1024卡集群训练效率提升3倍，功耗较上代下降40%，已开始批量供货。"},
    {"title": "开源大模型发布新版本", "url": "https://www.qbitai.com/b2", "source": "量子位",
     "summary": "新版本上下文窗口扩展至200万token，推理成本下降80%，在权威评测中得分提升12%。"},
    {"title": "国产GPU厂商发布新一代训练卡", "url": "https://www.qbitai.com/f1", "source": "量子位",
     "summary": "新一代训练卡单卡显存192GB，集群互联带宽提升4倍，已在多个智算中心完成适配验证。"},
    {"title": "阿里云发布行业大模型服务", "url": "https://www.ithome.com/c1", "source": "IT之家",
     "summary": "阿里云宣布面向制造业推出行业大模型服务，已服务1200家企业客户，平均交付周期缩短30%。"},
    {"title": "智驾方案商拿到新订单", "url": "https://www.ithome.com/e3", "source": "IT之家",
     "summary": "该方案商获得整车厂定点，订单涉及15款车型，生命周期内预计出货80万套。"},
    {"title": "AI医疗影像企业获三类证", "url": "https://www.ithome.com/f2", "source": "IT之家",
     "summary": "该企业AI影像辅助诊断软件获批三类医疗器械注册证，覆盖14种疾病，已进入300家医院。"},
    {"title": "国内具身智能创企完成B轮", "url": "https://www.qbitai.com/d3", "source": "量子位",
     "summary": "该创企完成B轮融资，募集8亿元，投后估值60亿元，资金将用于人形机器人量产产线建设。"},
    {"title": "宇树科技科创板挂牌", "url": "https://www.ithome.com/c2", "source": "IT之家",
     "summary": "宇树科技启动科创板申购，发行价150.80元每股，发行后市值约609.93亿元，发行市盈率219倍。"},
    {"title": "国产AI制药企业完成C轮融资", "url": "https://www.qbitai.com/d4", "source": "量子位",
     "summary": "该企业完成C轮融资，募集12亿元，投后估值90亿元，资金将用于药物研发算力平台扩容。"},
    # 以下为"应当被阶段 A 拦下"的素材（也会被选题，但候选会被过滤）
    {"title": "算力政策有新动向", "url": "https://www.ithome.com/g1", "source": "IT之家",
     "summary": "相关部门就智能算力布局发布新的指导意见，涉及算力枢纽节点的建设安排。"},
    {"title": "今日AI行业新闻早报汇总", "url": "https://www.ithome.com/g2", "source": "IT之家",
     "summary": "本期早报汇总了昨日AI行业的十余条动态，涵盖模型、芯片、政策与融资等方面。"},
]

INTL_MATERIAL = [
    {"title": "欧盟委员会正式通过人工智能法案实施条例", "url": "https://techcrunch.com/h1", "source": "TechCrunch",
     "summary": "The European Commission formally adopted the implementing regulation for the AI Act, "
                "covering general-purpose model obligations that take effect in phases from 2026."},
    {"title": "白宫发布人工智能行动计划", "url": "https://www.theverge.com/h2", "source": "The Verge",
     "summary": "The White House released an AI action plan directing federal agencies to accelerate "
                "procurement and expand federal compute infrastructure over the next fiscal year."},
    {"title": "英国政府发布人工智能安全监管新规", "url": "https://www.artificialintelligence-news.com/h3",
     "source": "AI News",
     "summary": "The UK government published new AI safety rules requiring frontier model developers "
                "to share safety test results with the regulator before deployment."},
    {"title": "Nvidia发布新一代数据中心GPU", "url": "https://venturebeat.com/i1", "source": "VentureBeat",
     "summary": "Nvidia unveiled its next-generation data center GPU, delivering 2.5 times the training "
                "throughput of the previous generation with 288GB of HBM memory."},
    {"title": "OpenAI发布新版本模型", "url": "https://openai.com/i2", "source": "OpenAI",
     "summary": "OpenAI released a new model version with 1.5 times faster reasoning and a 30% reduction "
                "in API cost, now available to enterprise customers."},
    {"title": "Anthropic发布企业级智能体功能", "url": "https://techcrunch.com/i3", "source": "TechCrunch",
     "summary": "Anthropic launched agentic capabilities for enterprise customers, supporting multi-step "
                "tasks across internal tools with a reported 40% reduction in completion time."},
    {"title": "微软与三星达成合作共建AI数据中心", "url": "https://www.theverge.com/j1", "source": "The Verge",
     "summary": "Microsoft and Samsung will co-build an AI data center in South Korea, with the first "
                "phase due next year and a planned capacity of 120 megawatts."},
    {"title": "OpenAI企业客户数量突破300万家", "url": "https://techcrunch.com/j2", "source": "TechCrunch",
     "summary": "OpenAI said its enterprise customer base surpassed 3 million organizations, up 60% "
                "year over year, with annualized revenue reaching 12 billion dollars."},
    {"title": "谷歌云新增AI算力区域", "url": "https://venturebeat.com/j3", "source": "VentureBeat",
     "summary": "Google Cloud added three new AI-optimized regions across Europe and Asia, bringing "
                "total capacity up 45% and cutting average latency for regional customers by 18%."},
    {"title": "Stripe 75亿美元收购OpenRouter", "url": "https://techcrunch.com/k1", "source": "TechCrunch",
     "summary": "Stripe agreed to acquire OpenRouter for $7.5 billion, the largest deal in the AI "
                "infrastructure space this year."},
    {"title": "Anthropic完成130亿美元新融资", "url": "https://techcrunch.com/k2", "source": "TechCrunch",
     "summary": "Anthropic raised $13 billion in new funding, valuing the company at $183 billion, "
                "led by sovereign funds."},
    {"title": "xAI完成新一轮融资", "url": "https://www.theverge.com/k3", "source": "The Verge",
     "summary": "xAI closed a new funding round of $6 billion at a $50 billion valuation, proceeds "
                "earmarked for expanding its Memphis training cluster."},
    # 坏素材：英文原句未翻译（候选会被拦）
    {"title": "Rapidly scaling online storage for AI clusters",
     "url": "https://techcrunch.com/g3", "source": "TechCrunch",
     "summary": "A vendor announced rapid scaling of online storage for AI clusters, targeting "
                "petabyte-scale workloads."},
]

MATERIAL = CN_MATERIAL + INTL_MATERIAL
VALID = {m["url"] for m in MATERIAL}


# ===========================================================================
# 阶段 A 返回：候选。每个地域 12 条合格（四类各 3）+ 4 条应被拦下的坏候选
# ===========================================================================
def _c(cat, title, source, url):
    return {"cat": cat, "title": title, "source": source, "url": url}


SEL_CN = [
    _c("policy", "两部门联合发布AI计量体系指引，破解测不准与数据荒", "IT之家", "https://www.ithome.com/a1"),
    _c("policy", "工信部印发算力互联行动计划，2027年建成50个枢纽节点", "IT之家", "https://www.ithome.com/a3"),
    _c("policy", "国家数据局印发数据要素三年行动方案，覆盖12个重点行业", "中国政府网", "https://www.gov.cn/zhengce/content_e2.htm"),
    _c("tech", "寒武纪发布新一代云端推理芯片，1024卡集群效率提升3倍", "量子位", "https://www.qbitai.com/b1"),
    _c("tech", "开源大模型新版本发布，上下文窗口扩展至200万token", "量子位", "https://www.qbitai.com/b2"),
    _c("tech", "国产GPU厂商发布训练卡，单卡显存192GB带宽提升4倍", "量子位", "https://www.qbitai.com/f1"),
    _c("industry", "阿里云推出制造业行业大模型，已服务1200家企业", "IT之家", "https://www.ithome.com/c1"),
    _c("industry", "智驾方案商获整车厂定点，涉及15款车型出货80万套", "IT之家", "https://www.ithome.com/e3"),
    _c("industry", "AI医疗影像软件获批三类证，覆盖14种疾病进300家医院", "IT之家", "https://www.ithome.com/f2"),
    _c("capital", "国内具身智能创企完成B轮，募集8亿元估值60亿元", "量子位", "https://www.qbitai.com/d3"),
    _c("capital", "宇树科技科创板挂牌，发行价150.80元每股", "IT之家", "https://www.ithome.com/c2"),
    _c("capital", "国产AI制药企业完成C轮，募集12亿元估值90亿元", "量子位", "https://www.qbitai.com/d4"),
    # ↓ 应被阶段 A 拦下
    _c("policy", "AI政策窗口开放", "IT之家", "https://www.ithome.com/g1"),          # 太短
    _c("industry", "早报：今日AI行业新闻汇总", "IT之家", "https://www.ithome.com/g2"),  # 聚合类
    _c("capital", "消息称某AI公司完成新一轮大额融资", "TechCrunch", "https://techcrunch.com/fake"),  # 编造 URL
    _c("tech", "有关部门就算力布局发布指导意见", "IT之家", "https://www.ithome.com/unused"),  # 编造 URL
]

SEL_INTL = [
    _c("policy", "欧盟委员会通过人工智能法案实施条例，分阶段生效", "TechCrunch", "https://techcrunch.com/h1"),
    _c("policy", "白宫发布人工智能行动计划，要求联邦机构加速采购", "The Verge", "https://www.theverge.com/h2"),
    _c("policy", "英国政府发布人工智能安全监管新规，前沿模型须报备", "AI News", "https://www.artificialintelligence-news.com/h3"),
    _c("tech", "Nvidia发布新一代数据中心GPU，训练吞吐提升2.5倍", "VentureBeat", "https://venturebeat.com/i1"),
    _c("tech", "OpenAI发布新版本模型，API成本下降30%", "OpenAI", "https://openai.com/i2"),
    _c("tech", "Anthropic推出企业级智能体功能，任务耗时缩短40%", "TechCrunch", "https://techcrunch.com/i3"),
    _c("industry", "微软与三星共建AI数据中心，一期容量120兆瓦", "The Verge", "https://www.theverge.com/j1"),
    _c("industry", "OpenAI企业客户突破300万家，同比增长60%", "TechCrunch", "https://techcrunch.com/j2"),
    _c("industry", "谷歌云新增三个AI算力区域，总容量提升45%", "VentureBeat", "https://venturebeat.com/j3"),
    _c("capital", "Stripe 75亿美元收购OpenRouter，AI基础设施最大并购", "TechCrunch", "https://techcrunch.com/k1"),
    _c("capital", "Anthropic完成130亿美元新融资，估值达1830亿美元", "TechCrunch", "https://techcrunch.com/k2"),
    _c("capital", "xAI完成60亿美元新融资，估值升至500亿美元", "The Verge", "https://www.theverge.com/k3"),
    # ↓ 应被阶段 A 拦下
    _c("tech", "Rapidly scaling online storage", "TechCrunch", "https://techcrunch.com/g3"),  # 英文原句
    _c("industry", "AI行业动态速览", "TechCrunch", "https://techcrunch.com/fake2"),            # 太短
    _c("capital", "某AI公司完成新一轮融资", "TechCrunch", "https://techcrunch.com/fake3"),      # 编造 URL
    _c("policy", "监管机构就人工智能发表看法", "TechCrunch", "https://techcrunch.com/fake4"),   # 编造 URL
]

# 补选场景：国内池第一轮 industry 只给 1 条；第二轮补足
SEL_CN_LACK_INDUSTRY = ([x for x in SEL_CN if x["cat"] == "policy"]
                        + [x for x in SEL_CN if x["cat"] == "tech"]
                        + [x for x in SEL_CN if x["cat"] == "capital"]
                        + [x for x in SEL_CN if x["cat"] == "industry"][:1])
SEL_CN_FILL_INDUSTRY = [x for x in SEL_CN if x["cat"] == "industry"][1:]

SEL_PLAN = {"cn": None, "intl": None}      # 每地域各一份轮次计划


def _region_of_prompt(prompt):
    """阶段 A 的 prompt 里写明了本批地域（见 build_select_prompt）。"""
    return "cn" if "「国内」" in prompt else "intl"


def _sel_items_for(prompt):
    region = _region_of_prompt(prompt)
    plan = SEL_PLAN.get(region)
    if not plan:
        return SEL_CN if region == "cn" else SEL_INTL
    idx = SELN.setdefault("round", {}).setdefault(region, 0)
    return plan[min(idx, len(plan) - 1)]


def _bump_sel_round(region):
    SELN.setdefault("round", {})[region] = \
        SELN.get("round", {}).get(region, 0) + 1


def _is_retry(prompt):
    """修复轮次的 prompt 特征：带【修改要求】区块。

    [2026-09-14] 指令区块由原来的"【上一轮不合格，本次必须逐条修正】"
    改为"【修改要求】…【修改要求结束】"（原因见 build_desc_prompt 的注释：
    原措辞被模型当成正文抄走了），判据同步更新。
    """
    return "【修改要求】" in prompt


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _is_selection(prompt):
    return "输出" in prompt and "选题清单" in prompt


def fake_urlopen(req, timeout=None):
    prompt = json.loads(req.data.decode("utf-8"))["messages"][0]["content"]
    sel = _is_selection(prompt)
    if sel:
        SELN["n"] += 1
        region = _region_of_prompt(prompt)
        ROUNDS.append({"kind": "A", "prompt": prompt, "region": region})
        body_summary = ("国内：政策落地、芯片与模型进展、具身智能融资。"
                        if region == "cn" else
                        "国外：欧盟AI法案落地、新GPU发布、大额并购与融资。")
        items = _sel_items_for(prompt)
        _bump_sel_round(region)
        content = json.dumps({"summary": body_summary, "items": items},
                             ensure_ascii=False)
        return _Resp({"choices": [{"message": {"content": content}}]})

    ROUNDS.append({"kind": "B", "prompt": prompt})

    # 阶段 B：写正文。desc prompt 里不含 URL（避免模型把 URL 抄进正文），
    # 因此按素材标题反查是哪一条。
    url = ""
    for m in MATERIAL:
        if m["title"] in prompt:
            url = m["url"]
            break
    mode = MODE["v"]
    good = LONG_DESC.get(url, SHORT_DESC)
    if mode == "retry_helps":
        # 听劝：带"字数不足"提示就写长
        desc = good if _is_retry(prompt) else SHORT_DESC
    elif mode == "number_fix":
        # 首次写长但带一个素材里没有的数字；被告知"数字对不上"后改掉
        if "数字对不上" in prompt:
            desc = good
        elif _is_retry(prompt):
            desc = SHORT_DESC
        else:
            desc = LONG_WITH_BAD_NUM.get(url, SHORT_DESC)
    elif mode == "leak_fix":
        # 复现 run 34819377914 第 4 条：把修改要求原文抄进正文。
        # 关键点——这条长度合格(>80)、数字也都对得上，只有 leak 检测能拦；
        # 改造前正是这类污染直接进了成品。
        if "混入了指令" in prompt:
            desc = good
        else:
            desc = good + "上一轮不合格，本次必须逐条修正。"
    elif mode == "vague_fix":
        # 复现同一次复验的第 6/7 条：结尾堆"引起广泛关注"这类凑字废话
        if "空泛表述" in prompt:
            desc = good
        else:
            desc = good + "此方案出台，引起广泛关注。"
    else:                                   # always_short：怎么说都写短
        # attempt=1 时"模型这回配合了"——验证外环重试后能恢复产出
        desc = good if ATTEMPT["v"] >= 1 else SHORT_DESC
    return _Resp({"choices": [{"message": {"content": desc}}]})


def _stats(items):
    tl = [len(i["title"]) for i in items]
    dl = [len(i["desc"]) for i in items]
    cell = {}
    for i in items:
        key = (i.get("region", "cn"), i["cat"])
        cell[key] = cell.get(key, 0) + 1
    return tl, dl, cell


# ===========================================================================
# 阶段 B 返回：8 月标准正文（100-160 字，数字全部取自摘要）
# ===========================================================================
SHORT_DESC = "相关部门发布了新的政策文件，涉及多项内容。"
LONG_DESC = {
    # —— 国内 ——
    "https://www.ithome.com/a1":
        "市场监管总局与国家发改委联合印发《人工智能计量体系和能力建设指引（2026版）》，"
        "围绕基础支撑、通用技术、核心技术等六大板块系统布局，聚焦算法黑箱和决策可解释性等痛点部署关键技术攻关，"
        "推动AI性能可测量、可比较、可追溯，并提出到2027年建成国家级计量技术研发应用中心。",
    "https://www.ithome.com/a3":
        "工业和信息化部印发智能算力互联互通行动计划，提出到2027年建成50个枢纽节点，"
        "跨区域算力调度时延下降30%，并统一算力标识与计量口径。"
        "行动计划要求打通不同主体算力资源的接入壁垒，推动算力并网与统一调度，"
        "同步建立算力交易与结算规则，为中小企业按需用算提供支撑。",
    "https://www.gov.cn/zhengce/content_e2.htm":
        "国家数据局印发《数据要素×三年行动方案》，覆盖12个重点行业，提出到2027年数据产业规模年均增速超过15%。"
        "方案围绕数据流通、场景开放与安全治理部署任务，明确支持人工智能训练数据的合规供给，"
        "并要求各地建立数据要素市场运行监测机制，为模型训练与行业应用提供稳定的数据来源。",
    "https://www.qbitai.com/b1":
        "该芯片面向数据中心推理场景发布，1024卡集群训练效率提升3倍，功耗较上代下降40%，目前已开始批量供货。"
        "厂商同步开放了配套软件栈，支持主流深度学习框架迁移，可降低存量集群的改造成本，"
        "进一步压缩单位算力的部署门槛。",
    "https://www.qbitai.com/b2":
        "新版本上下文窗口扩展至200万token，推理成本下降80%，在权威评测中综合得分提升12%，权重与技术报告同步开源。"
        "长上下文能力提升后，可直接处理完整代码仓库与长篇文档，减少分块拼接带来的信息损耗，"
        "为智能体类应用提供更稳定的基础。",
    "https://www.qbitai.com/f1":
        "国产GPU厂商发布新一代训练卡，单卡显存192GB，集群互联带宽较上代提升4倍，"
        "并已在多个智算中心完成适配验证。厂商同步升级了软件栈与算子库，"
        "支持主流训练框架的平滑迁移，旨在缓解大模型训练环节的算力供给压力。",
    "https://www.ithome.com/c1":
        "阿里云宣布面向制造业推出行业大模型服务，目前已服务1200家企业客户，平均交付周期缩短30%。"
        "服务覆盖设备预测性维护、工艺参数优化、质检等环节，并以订阅方式提供，"
        "降低中小制造企业的初始投入门槛。",
    "https://www.ithome.com/e3":
        "该智能驾驶方案商获得整车厂定点，订单涉及15款车型，生命周期内预计出货80万套。"
        "方案基于其自研的端到端感知与决策架构，可适配多种算力平台，"
        "定点意味着其已通过整车厂的功能安全与量产验证，后续将进入量产交付阶段。",
    "https://www.ithome.com/f2":
        "该企业人工智能影像辅助诊断软件获批三类医疗器械注册证，覆盖14种疾病，"
        "目前已进入300家医院投入使用。软件用于辅助医生识别影像中的可疑病灶并给出量化提示，"
        "注册证的取得意味着其可以合规进入院内收费环节，加快基层医疗机构的铺开节奏。",
    "https://www.qbitai.com/d3":
        "该创企完成B轮融资，募集8亿元，投后估值60亿元，资金将主要用于人形机器人量产产线建设与核心零部件自研。"
        "公司称其新一代机型已进入小批量交付阶段，本轮融资将支撑产能爬坡，"
        "并加快在工业与商用服务场景的验证。",
    "https://www.ithome.com/c2":
        "宇树科技正式启动科创板网上、网下申购，发行价150.80元每股，发行后市值约609.93亿元，发行市盈率219倍。"
        "公司是极少数在IPO前实现规模化盈利的全球人形机器人企业之一，"
        "本次募集资金将投向新一代人形机器人本体研发与产线建设，加快在工业与商用场景的交付节奏。",
    "https://www.qbitai.com/d4":
        "该企业完成C轮融资，募集12亿元，投后估值90亿元，资金将主要用于药物研发算力平台扩容与管线推进。"
        "公司目前已建成覆盖分子筛选与性质预测的一体化平台，服务多家药企的研发项目，"
        "本轮融资将支撑其算力规模翻倍。",
    # —— 国外 ——
    "https://techcrunch.com/h1":
        "欧盟委员会正式通过人工智能法案实施条例，明确了通用目的模型提供者的义务清单，"
        "相关要求将自2026年起分阶段生效。条例同时对系统性风险模型提出了评估与报告要求，"
        "并设置了过渡期安排，供企业在生效前完成合规整改。",
    "https://www.theverge.com/h2":
        "白宫发布人工智能行动计划，要求联邦机构在采购流程中加快引入人工智能方案，"
        "并扩大联邦算力基础设施建设。计划责成相关部门在下一财年提交执行进度报告，"
        "同时提出对本土供应链与安全评估环节的配套安排。相关部门将在未来数月内陆续公布执行细则，并明确各机构的进度报告口径与考核方式。",
    "https://www.artificialintelligence-news.com/h3":
        "英国政府发布人工智能安全监管新规，要求前沿模型开发者在部署前向监管机构提交安全测试结果，"
        "并建立事件报告机制。新规同时明确了监管机构的干预权限，"
        "为企业预留了合规过渡期。监管机构同时宣布将设立专门的人工智能安全审查团队，并在未来一年内分批公布配套的实施细则与指引文件。",
    "https://venturebeat.com/i1":
        "Nvidia发布新一代数据中心GPU，训练吞吐较上代提升2.5倍，单卡显存达到288GB。"
        "新卡同步升级了互联带宽与散热设计，面向大规模训练集群，"
        "并配套更新了软件栈以支持主流框架迁移，首批产品将于下季度交付。",
    "https://openai.com/i2":
        "OpenAI发布新版本模型，推理速度提升1.5倍，API调用成本下降30%，已面向企业客户开放。"
        "新版本在长时程任务上的稳定性有所改善，并扩大了上下文窗口，"
        "适用于智能体类应用的连续多步骤场景。公司同时更新了配套的开发者工具链，降低从旧版本迁移的改造成本。",
    "https://techcrunch.com/i3":
        "Anthropic面向企业客户推出智能体功能，支持跨内部系统的多步骤任务执行，"
        "官方称任务完成时间平均缩短40%。该功能提供权限控制与执行审计能力，"
        "并可与既有企业工作流集成，首批客户已在金融与软件行业落地。公司同时表示该功能将按席位与用量组合计费，未来几个季度逐步向更小的客户开放。",
    "https://www.theverge.com/j1":
        "微软与三星达成合作，双方将在韩国共建人工智能数据中心，一期预计明年投入使用，"
        "规划容量120兆瓦。项目由三星提供存储与先进封装环节的产品，"
        "主要服务亚太地区的企业客户，二期建设将视一期运行情况推进。双方同时计划在园区内配套建设研发中心，用于训练与推理场景的联合调试。",
    "https://techcrunch.com/j2":
        "OpenAI披露其企业客户数量已突破300万家，同比增长60%，年化收入达到120亿美元。"
        "公司表示企业侧需求主要集中在客服自动化与内部知识检索，"
        "并计划扩大行业解决方案团队以承接交付。分析认为企业侧需求正在从试用走向规模化采购，带动相关工具链与集成服务的支出同步上升。",
    "https://venturebeat.com/j3":
        "谷歌云宣布新增三个面向人工智能负载的算力区域，分布于欧洲与亚洲，"
        "总容量提升45%，区域客户平均时延下降18%。新区域配套部署了训练与推理加速实例，"
        "并支持数据本地化存储要求。新区域同时引入了碳排放更低的供电方案，以应对大规模训练任务的能耗压力。",
    "https://techcrunch.com/k1":
        "Stripe宣布以75亿美元收购OpenRouter，为今年人工智能基础设施领域规模最大的并购交易。"
        "OpenRouter主营多模型统一调用网关，聚合了数十家厂商的模型接口，"
        "收购后其路由与计量能力将并入Stripe的支付与计费体系。",
    "https://techcrunch.com/k2":
        "Anthropic完成130亿美元新一轮融资，投后估值达1830亿美元，本轮由多家主权基金领投。"
        "资金将主要用于扩充算力与加强安全研究，公司同时披露企业客户数量与年度经常性收入均较上一年度显著增长。",
    "https://www.theverge.com/k3":
        "xAI完成60亿美元新一轮融资，投后估值升至500亿美元，资金将用于扩建其训练集群。"
        "公司同时披露其模型调用量较上一季度实现翻倍，"
        "并计划扩大面向企业客户的API供给能力。本轮投资方同时获得董事会观察员席位，公司将按季度披露算力建设进度。",
}

# ---------- 阶段 B 返回：写入了摘要里查不到的数字（模拟模型顺手"推算"）----------
# "350亿元"在素材里完全不存在（素材里有 8亿元 / 60亿元 / 130亿美元），
# 必须被 ungrounded_numbers 抓出来并要求改写，而不是整条丢弃。
LONG_WITH_BAD_NUM = {
    u: d + "项目预计带动产业链投资350亿元。" for u, d in LONG_DESC.items()
}

ROUNDS = []       # 全部调用记录
# 模型行为模式：
#   retry_helps  首次短 → 收到"字数不足"提示后写长（正常模型）
#   number_fix   首次写长但编了个数字 → 收到"数字对不上"提示后改掉（可救援）
#   leak_fix     首次把修改要求原文抄进正文 → 收到"混入了指令"提示后写干净
#   vague_fix    首次结尾堆空泛评价 → 收到"空泛表述"提示后改掉
#   always_short 三轮都短，怎么说都不改 → 应当整批作废并触发选题重试
MODE = {"v": "retry_helps"}
SELN = {"n": 0}   # 累计"阶段 A 选题调用"次数（跨场景计数）
ATTEMPT = {"v": 0}  # 当前外环重试轮次（always_short 场景用它模拟"重试后配合"）


def main():
    np.urllib.request.urlopen = fake_urlopen
    np.call_glm.__globals__["urllib"] = np.urllib

    ok = True
    global ROUNDS

    # 阶段 B 的收工条件是"8 格各满 MIN_PER_CELL_DESC 条"，不是"总共写满 N 条"。
    expect_first = np.MIN_PER_CELL_DESC * len(np.CAT_LABELS) * len(np.REGIONS)   # 2×4×2 = 16

    def run(mode, plan_cn=None, plan_intl=None, attempt=0):
        ROUNDS.clear()
        SELN.clear()
        SELN["n"] = 0
        SELN["round"] = {}
        MODE["v"] = mode
        ATTEMPT["v"] = attempt
        SEL_PLAN["cn"] = plan_cn
        SEL_PLAN["intl"] = plan_intl
        r = np.generate_news(MATERIAL, "9月21日 星期一", attempt=attempt)
        SEL_PLAN["cn"] = None
        SEL_PLAN["intl"] = None
        return r

    # ================= 主场景：阶段 B 单条重写能救回来 =================
    print("=" * 74)
    print("主场景：阶段 B 首轮写得短 → 按“字数不足”提示重写 → 达标（国内8+国外8）")
    print("=" * 74)
    s1, items1 = run("retry_helps")

    sel_calls = [r for r in ROUNDS if r["kind"] == "A"]
    desc_calls = [r for r in ROUNDS if r["kind"] == "B"]
    n_desc_first = sum(1 for r in desc_calls if not _is_retry(r["prompt"]))
    n_desc_retry = sum(1 for r in desc_calls if _is_retry(r["prompt"]))

    print(f"\n调用构成：阶段 A {len(sel_calls)} 次（国内 "
          f"{sum(1 for r in sel_calls if r['region'] == 'cn')} / 国外 "
          f"{sum(1 for r in sel_calls if r['region'] == 'intl')}）｜ "
          f"阶段 B {len(desc_calls)} 次（首写 {n_desc_first} / 重写 {n_desc_retry}）")

    # A. 阶段 A 两个地域各放行 12 条（坏候选被拦），四类候选都够 3 条 → 各只跑 1 轮
    a_ok = (n_desc_first == expect_first)
    print(f"  {'✅' if a_ok else '❌'} 阶段 A 拦下 8 条坏候选；阶段 B 写到“8 格各 "
          f"{np.MIN_PER_CELL_DESC} 条”即收工（实得 {n_desc_first} 次首写 / 应有 {expect_first} 次）")
    ok &= a_ok
    a2_ok = len(sel_calls) == 2
    print(f"  {'✅' if a2_ok else '❌'} 两地域各选题 1 轮、无多余补选（阶段 A 共 {len(sel_calls)} 次）")
    ok &= a2_ok

    # B. 首写太短 → 每条都应触发一次重写
    b_ok = (n_desc_retry == expect_first)
    print(f"  {'✅' if b_ok else '❌'} 每条短正文都触发了重写，共 {n_desc_retry} 次")
    ok &= b_ok
    sample_retry = next((r["prompt"] for r in desc_calls if _is_retry(r["prompt"])), "")
    c_ok = "字数不足" in sample_retry and "不得自己编造" in sample_retry
    print(f"  {'✅' if c_ok else '❌'} 重写 prompt 含“字数不足须补细节 + 不得编造数字”")
    ok &= c_ok
    c2_ok = "不是新闻内容" in sample_retry
    print(f"  {'✅' if c2_ok else '❌'} 修改要求区块显式声明“不是新闻内容”")
    ok &= c2_ok

    tl, dl, cell = _stats(items1)
    n_cn = sum(1 for i in items1 if i.get("region") == "cn")
    n_intl = len(items1) - n_cn
    print(f"\n主场景结果 → {len(items1)} 条（国内 {n_cn} / 国外 {n_intl}）/ 目标 {np.MAX_ITEMS}")
    if items1:
        print(f"  标题字数 平均 {sum(tl)/len(tl):.1f}｜区间 {min(tl)}-{max(tl)}（8月基准 25.0）")
        print(f"  正文字数 平均 {sum(dl)/len(dl):.1f}｜区间 {min(dl)}-{max(dl)}（8月基准 118.9）")
        print(f"  8格分布 {cell}")
    cats = ("policy", "tech", "industry", "capital")
    for name, good in [
        (f"产出 {np.MAX_ITEMS} 条（国内 8 + 国外 8）",
         len(items1) == np.MAX_ITEMS and n_cn == 8 and n_intl == 8),
        ("标题均 ≥15 字", bool(tl) and min(tl) >= 15),
        ("正文均 ≥80 字（8月下沿）", bool(dl) and min(dl) >= 80),
        ("正文均 ≤400 字", bool(dl) and max(dl) <= 400),
        ("正文均值 ≥110 字", bool(dl) and sum(dl) / len(dl) >= 110),
        ("每条都带 region 字段", all(i.get("region") in np.REGIONS for i in items1)),
        ("两板块四类各 2 条（用户硬要求）",
         all(cell.get((r, c), 0) == 2 for r in np.REGIONS for c in cats)),
        ("无编造 URL", all(i["url"] in VALID for i in items1)),
    ]:
        print(f"  {'✅' if good else '❌'} {name}")
        ok &= good

    # ============ 救援场景：正文编了摘要里没有的数字 → 定向重写救回 ============
    print("\n" + "=" * 74)
    print("救援场景：首轮写出摘要里没有的数字 → 回灌“数字对不上”提示 → 救回整条")
    print("=" * 74)
    s3, items3 = run("number_fix")
    d_calls = [r for r in ROUNDS if r["kind"] == "B"]
    num_fix_calls = [r for r in d_calls if "数字对不上" in r["prompt"]]
    h_ok = len(num_fix_calls) == expect_first
    print(f"  {'✅' if h_ok else '❌'} 每条写了错数字的正文都被要求定向修正"
          f"（实得 {len(num_fix_calls)} 次 / 应有 {expect_first} 次）")
    ok &= h_ok
    sample_fix = num_fix_calls[0]["prompt"] if num_fix_calls else ""
    i_ok = ("350亿元" in sample_fix and "并没有" in sample_fix)
    print(f"  {'✅' if i_ok else '❌'} 修正提示指名道姓列出了具体那个数字（350亿元）")
    ok &= i_ok
    i2_ok = sample_fix.count("350亿元") == 1
    print(f"  {'✅' if i2_ok else '❌'} 金额不重复罗列（amt/mag 去重）")
    ok &= i2_ok
    j_ok = len(items3) == np.MAX_ITEMS and not any("350亿元" in it["desc"] for it in items3)
    print(f"  {'✅' if j_ok else '❌'} {expect_first} 条全部救回、残留错误数字 0 条")
    ok &= j_ok
    _, dl3, _ = _stats(items3)
    if dl3:
        k_ok = sum(dl3) / len(dl3) >= 110
        print(f"  {'✅' if k_ok else '❌'} 救回后正文均值 {sum(dl3)/len(dl3):.1f} 字仍达标")
        ok &= k_ok

    # ============ 抄指令场景：把修改要求原文抄进正文 → 定向重写清掉 ============
    print("\n" + "=" * 74)
    print("抄指令场景：正文里混入“上一轮不合格，本次必须逐条修正”→ leak 检测拦下并重写")
    print("=" * 74)
    _, items_leak = run("leak_fix")
    l_calls = [r for r in ROUNDS if r["kind"] == "B"]
    leak_fix_calls = [r for r in l_calls if "混入了指令" in r["prompt"]]
    l1_ok = len(leak_fix_calls) == expect_first
    print(f"  {'✅' if l1_ok else '❌'} 每条被污染的正文都被要求清掉指令文字"
          f"（实得 {len(leak_fix_calls)} 次 / 应有 {expect_first} 次）")
    ok &= l1_ok
    l2_ok = len(items_leak) == np.MAX_ITEMS and not any(
        np.prompt_leak(it["desc"]) for it in items_leak)
    print(f"  {'✅' if l2_ok else '❌'} 成品里指令残留 0 条（{len(items_leak)} 条）")
    ok &= l2_ok
    l3_ok = bool(leak_fix_calls) and "上一轮" in leak_fix_calls[0]["prompt"]
    print(f"  {'✅' if l3_ok else '❌'} 修复提示原文引用了被抓到的污染字眼（“上一轮”）")
    ok &= l3_ok

    # ============ 空泛评价场景：结尾堆废话 → 专项重写 ============
    print("\n" + "=" * 74)
    print("空泛评价场景：正文结尾堆“引起广泛关注”→ vague 检测拦下并重写")
    print("=" * 74)
    _, items_vague = run("vague_fix")
    v_calls = [r for r in ROUNDS if r["kind"] == "B"]
    vague_fix_calls = [r for r in v_calls if "空泛表述" in r["prompt"]]
    v1_ok = len(vague_fix_calls) == expect_first
    print(f"  {'✅' if v1_ok else '❌'} 每条写了空泛评价的正文都被要求改掉"
          f"（实得 {len(vague_fix_calls)} 次 / 应有 {expect_first} 次）")
    ok &= v1_ok
    v2_ok = len(items_vague) == np.MAX_ITEMS and not any(
        np.vague_phrases(it["desc"]) for it in items_vague)
    print(f"  {'✅' if v2_ok else '❌'} 成品里空泛评价残留 0 条（{len(items_vague)} 条）")
    ok &= v2_ok
    v3_ok = bool(vague_fix_calls) and "引起广泛关注" in vague_fix_calls[0]["prompt"]
    print(f"  {'✅' if v3_ok else '❌'} 修复提示点名具体那句废话（“引起广泛关注”）")
    ok &= v3_ok

    # ============ 补选场景：国内池某类候选不足 → 自动补选合并 ============
    print("\n" + "=" * 74)
    print("补选场景：国内池 industry 只给 1 条 → 自动补选一轮 → 8 格齐备")
    print("=" * 74)
    _, items_fill = run("retry_helps",
                        plan_cn=[SEL_CN_LACK_INDUSTRY, SEL_CN_FILL_INDUSTRY])
    a_rounds = [r for r in ROUNDS if r["kind"] == "A"]
    m1_ok = len(a_rounds) == 3      # 国内 2 轮 + 国外 1 轮
    print(f"  {'✅' if m1_ok else '❌'} 国内池 industry 候选不足触发补选"
          f"（阶段 A 共 {len(a_rounds)} 次：国内 2 / 国外 1）")
    ok &= m1_ok
    m2_ok = len(a_rounds) > 1 and "上一轮不合格，本次务必修正" in a_rounds[1]["prompt"]
    print(f"  {'✅' if m2_ok else '❌'} 补选轮 prompt 带“四类都要有候选”的纠正提示")
    ok &= m2_ok
    _, _, cell_fill = _stats(items_fill)
    m3_ok = (len(items_fill) == np.MAX_ITEMS
             and all(cell_fill.get((r, c), 0) >= 2
                     for r in np.REGIONS for c in cats))
    print(f"  {'✅' if m3_ok else '❌'} 补选后 8 格各 ≥2 条：{cell_fill}")
    ok &= m3_ok

    # ================= 外环场景：阶段 B 完全不改 → 触发选题重试 =================
    print("\n" + "=" * 74)
    print("外环场景：阶段 B 三轮都写短（模型完全不改）→ 0 条 → 选题重试")
    print("=" * 74)
    _, items2a = run("always_short", attempt=0)
    print(f"  attempt=0 → {len(items2a)} 条")
    e_ok = (len(items2a) == 0)
    print(f"  {'✅' if e_ok else '❌'} 全短文案被判废，未写入残缺内容")
    ok &= e_ok

    _, items2b = run("always_short", attempt=1)
    sel1 = [r for r in ROUNDS if r["kind"] == "A"][0]["prompt"]
    f_ok = "上一轮不合格，本次务必修正" in sel1
    print(f"  {'✅' if f_ok else '❌'} attempt=1 的选题 prompt 含“上一轮不合格，本次务必修正”")
    ok &= f_ok
    print(f"  attempt=1 → {len(items2b)} 条")
    g_ok = len(items2b) == np.MAX_ITEMS
    print(f"  {'✅' if g_ok else '❌'} 重试后恢复到 {np.MAX_ITEMS} 条")
    ok &= g_ok

    print("\n" + ("🎉 两阶段 mock 演练全部通过" if ok else "⚠ 存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
