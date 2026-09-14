#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端 mock 演练（不打真实 API）：验证两阶段生成链与 8 月标准的过滤/重试路径。

链路已改为两阶段（2026-09-14）：
  阶段 A 选题：一次调用产出候选（只含 cat/title/source/url，不写正文）
  阶段 B 写正文：逐条素材单独调用，产出 110-150 字 desc；
                 不合格不再直接丢弃，而是按体检结果【定向重写】至多 3 轮

三个场景（都模拟真实会发生的失败）：
  [主场景] 阶段 B 首次写得太短 → 按"字数不足"提示重写 → 达标 → 稳定产出 8 条
  [救援场景] 阶段 B 首次写了摘要里没有的数字 → 按"数字对不上"提示重写 → 救回
  [外环]   阶段 B 三次都太短（模型完全不改）→ 0 条 → 触发选题重试（attempt=1）

断言重点：
  A. 阶段 A 的短标题/聚合类/编造 URL/英文原句候选被拦下，合格候选才进阶段 B
  B. 阶段 B 单条重写时，prompt 必须含针对性修正提示（否则重写是空转）
  C. 数字对不上时回灌的是【具体数字名】的修复提示，而不是笼统"重写"
  D. 外环重试 prompt 必须含"上一轮不合格，本次务必修正"提示
  E. 最终 8 条、四类均衡、标题与正文均落在 8 月标准区间
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("ZHIPU_API_KEY", "dummy")

import news_pipeline as np  # noqa: E402

# ---------- 素材（含摘要，数字都可溯源）----------
MATERIAL = [
    {"title": "两部门联合发布AI计量体系指引", "url": "https://www.ithome.com/a1", "source": "IT之家",
     "summary": "市场监管总局与国家发改委联合印发《人工智能计量体系和能力建设指引（2026版）》，围绕六大板块系统布局，提出到2027年建成国家级计量技术研发应用中心。"},
    {"title": "交通部发布AI+交通场景方案", "url": "https://www.ithome.com/a2", "source": "IT之家",
     "summary": "交通运输部发布人工智能应用场景方案，共涉及41个具体场景，覆盖公路、铁路、水运等领域，提出分阶段推进目标。"},
    {"title": "某AI芯片厂商发布数据中心新品", "url": "https://www.qbitai.com/b1", "source": "量子位",
     "summary": "该芯片面向数据中心推理场景，1024卡集群训练效率提升3倍，功耗较上代下降40%，已开始批量供货。"},
    {"title": "开源大模型发布新版本", "url": "https://www.qbitai.com/b2", "source": "量子位",
     "summary": "新版本上下文窗口扩展至200万token，推理成本下降80%，在权威评测中得分提升12%。"},
    {"title": "阿里云发布行业大模型服务", "url": "https://www.ithome.com/c1", "source": "IT之家",
     "summary": "阿里云宣布面向制造业推出行业大模型服务，已服务1200家企业客户，平均交付周期缩短30%。"},
    {"title": "宇树科技科创板挂牌", "url": "https://www.ithome.com/c2", "source": "IT之家",
     "summary": "宇树科技启动科创板申购，发行价150.80元每股，发行后市值约609.93亿元，发行市盈率219倍。"},
    {"title": "Stripe 75亿美元收购OpenRouter", "url": "https://techcrunch.com/d1", "source": "TechCrunch",
     "summary": "Stripe agreed to acquire OpenRouter for $7.5 billion, the largest deal in the AI infrastructure space this year."},
    {"title": "Anthropic完成130亿美元新融资", "url": "https://techcrunch.com/d2", "source": "TechCrunch",
     "summary": "Anthropic raised $13 billion in new funding, valuing the company at $183 billion, led by sovereign funds."},
    {"title": "国内具身智能创企完成B轮", "url": "https://www.qbitai.com/d3", "source": "量子位",
     "summary": "该创企完成B轮融资，募集8亿元，投后估值60亿元，资金将用于人形机器人量产产线建设。"},
    {"title": "数据中心液冷方案落地", "url": "https://www.theverge.com/e1", "source": "The Verge",
     "summary": "The company deployed liquid cooling across 12 data centers, cutting PUE to 1.08 and saving 25% power."},
    {"title": "欧洲AI法案实施细则公布", "url": "https://www.theverge.com/e2", "source": "The Verge",
     "summary": "European regulators published implementation rules covering 55 high-risk AI systems, effective from next year."},
    {"title": "智驾方案商拿到新订单", "url": "https://www.ithome.com/e3", "source": "IT之家",
     "summary": "该方案商获得整车厂定点，订单涉及15款车型，生命周期内预计出货80万套。"},
    {"title": "欧洲AI法案实施细则公布", "url": "https://www.theverge.com/e2", "source": "The Verge",
     "summary": "European regulators published implementation rules covering 55 high-risk AI systems, effective from next year."},
    {"title": "国产GPU厂商发布新一代训练卡", "url": "https://www.qbitai.com/f1", "source": "量子位",
     "summary": "新一代训练卡单卡显存192GB，集群互联带宽提升4倍，已在多个智算中心完成适配验证。"},
    {"title": "AI医疗影像企业获三类证", "url": "https://www.ithome.com/f2", "source": "IT之家",
     "summary": "该企业AI影像辅助诊断软件获批三类医疗器械注册证，覆盖14种疾病，已进入300家医院。"},
    {"title": "Rapidly scaling online storage for AI clusters",
     "url": "https://techcrunch.com/g3", "source": "TechCrunch",
     "summary": "A vendor announced rapid scaling of online storage for AI clusters, targeting petabyte-scale workloads."},
    {"title": "算力政策有新动向", "url": "https://www.ithome.com/g1", "source": "IT之家",
     "summary": "相关部门就智能算力布局发布新的指导意见，涉及算力枢纽节点的建设安排。"},
    {"title": "今日AI行业新闻早报汇总", "url": "https://www.ithome.com/g2", "source": "IT之家",
     "summary": "本期早报汇总了昨日AI行业的十余条动态，涵盖模型、芯片、政策与融资等方面。"},
]
VALID = {m["url"] for m in MATERIAL}

# ---------- 阶段 A 返回：候选（含应被拦下的坏标题 + 1 条编造 URL）----------
SEL_ITEMS = [
    {"cat": "policy", "title": "两部门联合发布AI计量体系指引，破解测不准与数据荒",
     "source": "IT之家", "url": "https://www.ithome.com/a1"},
    {"cat": "policy", "title": "交通部发布AI+交通场景方案41个，覆盖公路铁路水运",
     "source": "IT之家", "url": "https://www.ithome.com/a2"},
    {"cat": "tech", "title": "国产数据中心芯片发布，1024卡集群训练效率提升3倍",
     "source": "量子位", "url": "https://www.qbitai.com/b1"},
    {"cat": "tech", "title": "开源大模型新版本发布，上下文扩展至200万token",
     "source": "量子位", "url": "https://www.qbitai.com/b2"},
    {"cat": "industry", "title": "阿里云推出制造业行业大模型，已服务1200家企业",
     "source": "IT之家", "url": "https://www.ithome.com/c1"},
    {"cat": "industry", "title": "宇树科技科创板挂牌，人形机器人第一股诞生",
     "source": "IT之家", "url": "https://www.ithome.com/c2"},
    {"cat": "capital", "title": "Stripe 75亿美元收购OpenRouter，AI基础设施最大并购",
     "source": "TechCrunch", "url": "https://techcrunch.com/d1"},
    {"cat": "capital", "title": "Anthropic完成130亿美元新融资，估值达1830亿美元",
     "source": "TechCrunch", "url": "https://techcrunch.com/d2"},
    {"cat": "capital", "title": "国内具身智能创企完成B轮，募集8亿元估值60亿元",
     "source": "量子位", "url": "https://www.qbitai.com/d3"},
    {"cat": "tech", "title": "数据中心液冷规模化落地，PUE降至1.08节电25%",
     "source": "The Verge", "url": "https://www.theverge.com/e1"},
    {"cat": "policy", "title": "欧洲AI法案实施细则公布，55个高风险系统纳入监管",
     "source": "The Verge", "url": "https://www.theverge.com/e2"},
    {"cat": "industry", "title": "智驾方案商获整车厂定点，涉及15款车型出货80万套",
     "source": "IT之家", "url": "https://www.ithome.com/e3"},
    {"cat": "tech", "title": "国产GPU厂商发布训练卡，单卡显存192GB带宽提升4倍",
     "source": "量子位", "url": "https://www.qbitai.com/f1"},
    {"cat": "industry", "title": "AI医疗影像软件获批三类证，覆盖14种疾病进300家医院",
     "source": "IT之家", "url": "https://www.ithome.com/f2"},
    # ↓ 以下 4 条应被阶段 A 拦下（不进入阶段 B，不消耗写正文的调用）
    {"cat": "policy", "title": "AI政策窗口开放",                      # 太短(8字)
     "source": "IT之家", "url": "https://www.ithome.com/g1"},
    {"cat": "industry", "title": "早报：今日AI行业新闻汇总",           # 聚合类
     "source": "IT之家", "url": "https://www.ithome.com/g2"},
    {"cat": "capital", "title": "消息称某AI公司完成新一轮大额融资",
     "source": "TechCrunch", "url": "https://techcrunch.com/fake"},    # 编造 URL
    {"cat": "tech", "title": "Rapidly scaling online storage",        # 英文原句未翻译
     "source": "TechCrunch", "url": "https://techcrunch.com/g3"},
]

# ---------- 阶段 B 返回：短文案（模拟免费模型写成 20-30 字）----------
SHORT_DESC = "相关部门发布了新的政策文件，涉及多项内容。"
# ---------- 阶段 B 返回：8 月标准正文（100-160 字，数字全部取自摘要）----------
LONG_DESC = {
    "https://www.ithome.com/a1":
        "市场监管总局与国家发改委联合印发《人工智能计量体系和能力建设指引（2026版）》，"
        "围绕基础支撑、通用技术、核心技术等六大板块系统布局，聚焦算法黑箱和决策可解释性等痛点部署关键技术攻关，"
        "推动AI性能可测量、可比较、可追溯，并提出到2027年建成国家级计量技术研发应用中心。",
    "https://www.ithome.com/a2":
        "交通运输部发布人工智能应用场景方案，共涉及41个具体场景，覆盖公路、铁路、水运等重点领域，"
        "并明确了分阶段推进目标。方案要求加快智能感知、车路协同等技术在典型场景落地，"
        "同步完善数据安全与责任认定规则，为后续规模化推广提供依据。",
    "https://www.qbitai.com/b1":
        "该芯片面向数据中心推理场景发布，1024卡集群训练效率提升3倍，功耗较上代下降40%，目前已开始批量供货。"
        "厂商同步开放了配套软件栈，支持主流深度学习框架迁移，可降低存量集群的改造成本，"
        "进一步压缩单位算力的部署门槛。",
    "https://www.qbitai.com/b2":
        "新版本上下文窗口扩展至200万token，推理成本下降80%，在权威评测中综合得分提升12%，权重与技术报告同步开源。"
        "长上下文能力提升后，可直接处理完整代码仓库与长篇文档，减少分块拼接带来的信息损耗，"
        "为智能体类应用提供更稳定的基础。",
    "https://www.ithome.com/c1":
        "阿里云宣布面向制造业推出行业大模型服务，目前已服务1200家企业客户，平均交付周期缩短30%。"
        "服务覆盖设备预测性维护、工艺参数优化、质检等环节，并以订阅方式提供，"
        "降低中小制造企业的初始投入门槛。",
    "https://www.ithome.com/c2":
        "宇树科技正式启动科创板网上、网下申购，发行价150.80元每股，发行后市值约609.93亿元，发行市盈率219倍。"
        "公司是极少数在IPO前实现规模化盈利的全球人形机器人企业之一，"
        "本次募集资金将投向新一代人形机器人本体研发与产线建设，加快在工业与商用场景的交付节奏。",
    "https://techcrunch.com/d1":
        "Stripe 宣布以75亿美元收购 OpenRouter，为今年 AI 基础设施领域规模最大的并购交易。"
        "OpenRouter 主营多模型统一调用网关，聚合了数十家厂商的模型接口，"
        "收购后其路由与计量能力将并入 Stripe 的支付与计费体系，有望形成按调用量计费的一体化方案。",
    "https://techcrunch.com/d2":
        "Anthropic 完成130亿美元新一轮融资，投后估值达1830亿美元，本轮由多家主权基金领投。"
        "资金将主要用于扩充算力与加强安全研究，公司同时披露企业客户数量与年度经常性收入均较上一年度显著增长，"
        "成为全球估值最高的AI模型公司之一。",
    "https://www.qbitai.com/d3":
        "该创企完成B轮融资，募集8亿元，投后估值60亿元，资金将主要用于人形机器人量产产线建设与核心零部件自研。"
        "公司称其新一代机型已进入小批量交付阶段，本轮融资将支撑产能爬坡，"
        "并加快在工业与商用服务场景的验证。",
    "https://www.theverge.com/e1":
        "该企业在12个数据中心完成液冷方案部署，PUE降至1.08，整体节电25%。"
        "方案采用冷板与浸没两条技术路线并行，适配高功率密度机柜，并配套余热回收系统。"
        "随着单机柜功率持续攀升，液冷正从试点走向规模化，成为新建智算中心的主流选择。",
    "https://www.theverge.com/e2":
        "欧洲监管机构公布人工智能法案实施细则，将55个高风险人工智能系统纳入监管范围，"
        "相关要求自明年起生效。细则明确了提供方的合规评估义务、技术文档留存要求，"
        "以及在公共场景部署前需完成的第三方符合性评价流程，为成员国统一执法口径提供依据。",
    "https://www.ithome.com/e3":
        "该智能驾驶方案商获得整车厂定点，订单涉及15款车型，生命周期内预计出货80万套。"
        "方案基于其自研的端到端感知与决策架构，可适配多种算力平台，"
        "定点意味着其已通过整车厂的功能安全与量产验证，后续将进入量产交付阶段。",
    "https://www.qbitai.com/f1":
        "国产GPU厂商发布新一代训练卡，单卡显存192GB，集群互联带宽较上代提升4倍，"
        "并已在多个智算中心完成适配验证。厂商同步升级了软件栈与算子库，"
        "支持主流训练框架的平滑迁移，旨在缓解大模型训练环节的算力供给压力。",
    "https://www.ithome.com/f2":
        "该企业人工智能影像辅助诊断软件获批三类医疗器械注册证，覆盖14种疾病，"
        "目前已进入300家医院投入使用。软件用于辅助医生识别影像中的可疑病灶并给出量化提示，"
        "注册证的取得意味着其可以合规进入院内收费环节，加快基层医疗机构的铺开节奏。",
}

# ---------- 阶段 B 返回：写入了摘要里查不到的数字（模拟模型顺手"推算"）----------
# "120亿元"在素材里完全不存在（素材里有 8亿元 / 60亿元 / 130亿美元），
# 必须被 ungrounded_numbers 抓出来并要求改写，而不是整条丢弃。
LONG_WITH_BAD_NUM = {
    u: d + "项目预计带动产业链投资120亿元。" for u, d in LONG_DESC.items()
}

ROUNDS = []       # 全部调用记录
# 模型行为模式：
#   retry_helps  首次短 → 收到"字数不足"提示后写长（正常模型）
#   number_fix   首次写长但编了个数字 → 收到"数字对不上"提示后改掉（可救援）
#   always_short 三轮都短，怎么说都不改 → 应当整批作废并触发选题重试
MODE = {"v": "retry_helps"}
SELN = {"n": 0}   # 累计"阶段 A 选题调用"次数（跨场景计数，用于判断是否已是重试轮）


def _is_retry(prompt):
    """"正文待修"轮次的 prompt 特征：带【上一轮不合格，本次必须逐条修正】段落。"""
    return "上一轮不合格" in prompt or "上一轮太短" in prompt


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
    return "输出选题清单" in prompt


def fake_urlopen(req, timeout=None):
    prompt = json.loads(req.data.decode("utf-8"))["messages"][0]["content"]
    sel = _is_selection(prompt)
    if sel:
        SELN["n"] += 1
    sel_round = SELN["n"]
    ROUNDS.append({"kind": "A" if sel else "B", "prompt": prompt,
                   "sel_round": sel_round})

    if sel:
        body_summary = "今日AI产业要闻：政策落地、芯片与模型进展、大额并购与具身智能融资。"
        content = json.dumps({"summary": body_summary, "items": SEL_ITEMS},
                             ensure_ascii=False)
        return _Resp({"choices": [{"message": {"content": content}}]})

    # 阶段 B：写正文。desc prompt 里不含 URL（避免模型把 URL 抄进正文），
    # 因此按素材标题反查是哪一条。
    url = ""
    for m in MATERIAL:
        if m["title"] in prompt:
            url = m["url"]
            break
    mode = MODE["v"]
    if mode == "retry_helps":
        # 听劝：带"字数不足"提示就写长
        desc = LONG_DESC.get(url, SHORT_DESC) if _is_retry(prompt) else SHORT_DESC
    elif mode == "number_fix":
        # 首次写长但带一个素材里没有的数字；被告知"数字对不上"后改掉
        if "数字对不上" in prompt:
            desc = LONG_DESC.get(url, SHORT_DESC)
        elif _is_retry(prompt):
            desc = SHORT_DESC
        else:
            desc = LONG_WITH_BAD_NUM.get(url, SHORT_DESC)
    else:                                   # always_short：怎么说都写短
        desc = LONG_DESC.get(url, SHORT_DESC) if sel_round >= 2 else SHORT_DESC
    return _Resp({"choices": [{"message": {"content": desc}}]})


def _stats(items):
    tl = [len(i["title"]) for i in items]
    dl = [len(i["desc"]) for i in items]
    dist = {}
    for i in items:
        dist[i["cat"]] = dist.get(i["cat"], 0) + 1
    return tl, dl, dist


def main():
    np.urllib.request.urlopen = fake_urlopen
    np.call_glm.__globals__["urllib"] = np.urllib

    ok = True
    global ROUNDS

    # ================= 主场景：阶段 B 单条重写能救回来 =================
    print("=" * 74)
    print("主场景：阶段 B 首轮写得短 → 按“字数不足”提示重写 → 达标")
    print("=" * 74)
    ROUNDS.clear()
    MODE["v"] = "retry_helps"
    s1, items1 = np.generate_news(MATERIAL, "9月14日 星期一", attempt=0)

    sel0 = [r for r in ROUNDS if r["kind"] == "A"][0]["prompt"]
    desc_calls = [r for r in ROUNDS if r["kind"] == "B"]
    n_desc_first = sum(1 for r in desc_calls if not _is_retry(r["prompt"]))
    n_desc_retry = sum(1 for r in desc_calls if _is_retry(r["prompt"]))

    print(f"\n调用构成：阶段 A 1 次 ｜ 阶段 B {len(desc_calls)} 次"
          f"（首写 {n_desc_first} / 重写 {n_desc_retry}）")
    # A. 阶段 A 只应放行 14 条（短标题/聚合类/英文原句/编造 URL 被拦），
    #    且阶段 B 写满 DESC_TARGET 就停手，不再为剩余候选白烧调用
    a_ok = (n_desc_first == np.DESC_TARGET)
    print(f"  {'✅' if a_ok else '❌'} 阶段 A 拦下 4 条坏候选，写正文到 {np.DESC_TARGET} 条即停"
          f"（实得 {n_desc_first} 次首写）")
    ok &= a_ok

    # B. 首写太短 → 每条都应触发一次重写
    b_ok = (n_desc_retry == np.DESC_TARGET)
    print(f"  {'✅' if b_ok else '❌'} 每条短正文都触发了重写，共 {n_desc_retry} 次")
    ok &= b_ok
    sample_retry = next((r["prompt"] for r in desc_calls if _is_retry(r["prompt"])), "")
    c_ok = "字数不足" in sample_retry and "不得自己编造" in sample_retry
    print(f"  {'✅' if c_ok else '❌'} 重写 prompt 含“字数不足须补细节 + 不得编造数字”")
    ok &= c_ok

    # 首轮选题 prompt 不应含重试提示（避免误导）
    d_ok = "上一轮不合格" not in sel0
    print(f"  {'✅' if d_ok else '❌'} 首轮选题 prompt 不含重试提示")
    ok &= d_ok

    tl, dl, dist = _stats(items1)
    print(f"\n主场景结果 → {len(items1)} 条 / 目标 8")
    if items1:
        print(f"  标题字数 平均 {sum(tl)/len(tl):.1f}｜区间 {min(tl)}-{max(tl)}（8月基准 25.0）")
        print(f"  正文字数 平均 {sum(dl)/len(dl):.1f}｜区间 {min(dl)}-{max(dl)}（8月基准 118.9）")
        print(f"  四类分布 {dist}")
    for name, good in [
        ("产出 8 条", len(items1) == 8),
        ("标题均 ≥15 字", bool(tl) and min(tl) >= 15),
        ("正文均 ≥80 字（8月下沿）", bool(dl) and min(dl) >= 80),
        ("正文均 ≤400 字", bool(dl) and max(dl) <= 400),
        ("正文均值 ≥110 字", bool(dl) and sum(dl) / len(dl) >= 110),
        ("四类齐全", set(dist) == {"policy", "tech", "industry", "capital"}),
        ("每类 ≥1 条", all(dist.get(c, 0) >= 1 for c in ("policy", "tech", "industry", "capital"))),
        ("无编造 URL", all(i["url"] in VALID for i in items1)),
    ]:
        print(f"  {'✅' if good else '❌'} {name}")
        ok &= good

    # ============ 救援场景：正文编了摘要里没有的数字 → 定向重写救回 ============
    print("\n" + "=" * 74)
    print("救援场景：首轮写出摘要里没有的数字 → 回灌“数字对不上”提示 → 救回整条")
    print("=" * 74)
    ROUNDS.clear()
    SELN["n"] = 0
    MODE["v"] = "number_fix"
    s3, items3 = np.generate_news(MATERIAL, "9月14日 星期一", attempt=0)
    d_calls = [r for r in ROUNDS if r["kind"] == "B"]
    num_fix_calls = [r for r in d_calls if "数字对不上" in r["prompt"]]
    h_ok = len(num_fix_calls) == np.DESC_TARGET
    print(f"  {'✅' if h_ok else '❌'} 每条写了错数字的正文都被要求定向修正"
          f"（实得 {len(num_fix_calls)} 次 / 应有 {np.DESC_TARGET} 次）")
    ok &= h_ok
    sample_fix = num_fix_calls[0]["prompt"] if num_fix_calls else ""
    i_ok = ("120亿元" in sample_fix and "并没有" in sample_fix)
    print(f"  {'✅' if i_ok else '❌'} 修正提示指名道姓列出了具体那个数字（120亿元）")
    ok &= i_ok
    # 同一金额的 amt:/mag: 双 token 不应重复罗列
    i2_ok = sample_fix.count("120亿元") == 1
    print(f"  {'✅' if i2_ok else '❌'} 金额不重复罗列（amt/mag 去重）")
    ok &= i2_ok
    j_ok = len(items3) == 8 and not any("120亿元" in it["desc"] for it in items3)
    print(f"  {'✅' if j_ok else '❌'} 12 条全部救回、残留错误数字 0 条")
    ok &= j_ok
    _, dl3, dist3 = _stats(items3)
    if dl3:
        k_ok = sum(dl3) / len(dl3) >= 110
        print(f"  {'✅' if k_ok else '❌'} 救回后正文均值 {sum(dl3)/len(dl3):.1f} 字仍达标")
        ok &= k_ok

    # ================= 外环场景：阶段 B 完全不改 → 触发选题重试 =================
    print("\n" + "=" * 74)
    print("外环场景：阶段 B 三轮都写短（模型完全不改）→ 0 条 → 选题重试")
    print("=" * 74)
    ROUNDS.clear()
    MODE["v"] = "always_short"
    SELN["n"] = 0
    s2a, items2a = np.generate_news(MATERIAL, "9月14日 星期一", attempt=0)
    print(f"  attempt=0 → {len(items2a)} 条")
    e_ok = (len(items2a) == 0)
    print(f"  {'✅' if e_ok else '❌'} 全短文案被判废，未写入残缺内容")
    ok &= e_ok

    ROUNDS.clear()
    s2b, items2b = np.generate_news(MATERIAL, "9月14日 星期一", attempt=1)
    sel1 = [r for r in ROUNDS if r["kind"] == "A"][0]["prompt"]
    f_ok = "上一轮不合格，本次务必修正" in sel1
    print(f"  {'✅' if f_ok else '❌'} attempt=1 的选题 prompt 含“上一轮不合格，本次务必修正”")
    ok &= f_ok
    print(f"  attempt=1 → {len(items2b)} 条")
    g_ok = len(items2b) == 8
    print(f"  {'✅' if g_ok else '❌'} 重试后恢复到 8 条")
    ok &= g_ok

    print("\n" + ("🎉 两阶段 mock 演练全部通过" if ok else "⚠ 存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
