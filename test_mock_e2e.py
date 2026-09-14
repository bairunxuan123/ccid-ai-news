#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端 mock 演练（不打真实 API）：验证 8 月标准的过滤链与重试修正路径。

场景设计（模拟真实会发生的失败）：
  第 1 轮：模型只写了短文案（正文约 30 字）→ 应被字数硬校验全部丢弃 → 触发重试
  第 2 轮：模型按重试提示词修正，写足字数 → 应稳定产出 8 条

断言重点：
  A. 第 1 轮确实因"正文过短"被丢弃，且条数不足触发重试
  B. 第 2 轮拿到的 prompt 里必须含新增的"字数不达标"修正提示
     （若没有，说明重试是空转 —— 这正是本轮修复的缺陷）
  C. 最终 8 条、四类均衡、标题与正文均落在 8 月标准区间
"""
import json
import os
import re
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
]
VALID = {m["url"] for m in MATERIAL}


def _mk(cat, title, desc, src, url):
    return {"cat": cat, "title": title, "desc": desc, "source": src, "url": url}


# ---------- 第 1 轮：短文案（模拟 9 月的退化写法）----------
SHORT_ITEMS = [
    _mk("policy", "AI政策窗口开放", "相关部门发布了新的政策文件。", "IT之家", "https://www.ithome.com/a1"),
    _mk("policy", "发布脑机接口标准", "有关部门发布了标准。", "IT之家", "https://www.ithome.com/a2"),
    _mk("tech", "Nvidia解释增长原因", "公司解释了业绩增长的原因。", "量子位", "https://www.qbitai.com/b1"),
    _mk("tech", "新模型开源", "某公司开源了新模型。", "量子位", "https://www.qbitai.com/b2"),
    _mk("industry", "某公司面临挑战", "该公司当前面临一些挑战。", "IT之家", "https://www.ithome.com/c1"),
    _mk("industry", "厂商发布新品", "厂商发布了新的产品。", "IT之家", "https://www.ithome.com/c2"),
    _mk("capital", "企业完成融资", "企业完成了新一轮融资。", "TechCrunch", "https://techcrunch.com/d1"),
    _mk("capital", "AI公司获得投资", "该公司获得了投资。", "TechCrunch", "https://techcrunch.com/d2"),
    _mk("tech", "AI供电架构问题", "供电架构存在一些问题。", "The Verge", "https://www.theverge.com/e1"),
    _mk("industry", "企业达成合作", "双方达成了合作协议。", "IT之家", "https://www.ithome.com/e3"),
]

# ---------- 第 2 轮：按 8 月标准修正后的文案 ----------
LONG_ITEMS = [
    _mk("policy", "两部门联合发布AI计量体系指引，破解测不准与数据荒",
        "市场监管总局与国家发改委联合印发《人工智能计量体系和能力建设指引（2026版）》，围绕基础支撑、通用技术、核心技术等六大板块系统布局，"
        "聚焦算法黑箱和决策可解释性等痛点部署关键技术攻关，推动AI性能可测量、可比较、可追溯。指引提出到2027年建成国家级计量技术研发应用中心，"
        "打通实验室到行业应用的最后一公里。",
        "IT之家", "https://www.ithome.com/a1"),
    _mk("policy", "交通部发布AI+交通场景方案41个，覆盖公路铁路水运",
        "交通运输部发布人工智能应用场景方案，共涉及41个具体场景，覆盖公路、铁路、水运等重点领域，并明确了分阶段推进目标。"
        "方案要求加快智能感知、车路协同等技术在典型场景落地，同步完善数据安全与责任认定规则，为后续规模化推广提供依据。",
        "IT之家", "https://www.ithome.com/a2"),
    _mk("tech", "国产数据中心芯片发布，1024卡集群训练效率提升3倍",
        "该芯片面向数据中心推理场景发布，1024卡集群训练效率提升3倍，功耗较上代下降40%，目前已开始批量供货。"
        "厂商同步开放了配套软件栈，支持主流深度学习框架迁移，可降低存量集群的改造成本，进一步压缩单位算力的部署门槛。",
        "量子位", "https://www.qbitai.com/b1"),
    _mk("tech", "开源大模型新版本发布，上下文扩展至200万token",
        "新版本上下文窗口扩展至200万token，推理成本下降80%，在权威评测中综合得分提升12%，权重与技术报告同步开源。"
        "长上下文能力提升后，可直接处理完整代码仓库与长篇文档，减少分块拼接带来的信息损耗，为智能体类应用提供更稳定的基础。",
        "量子位", "https://www.qbitai.com/b2"),
    _mk("industry", "阿里云推出制造业行业大模型，已服务1200家企业",
        "阿里云宣布面向制造业推出行业大模型服务，目前已服务1200家企业客户，平均交付周期缩短30%。"
        "服务覆盖设备预测性维护、工艺参数优化、质检等环节，并以订阅方式提供，降低中小制造企业的初始投入门槛。",
        "IT之家", "https://www.ithome.com/c1"),
    _mk("industry", "宇树科技科创板挂牌，人形机器人第一股诞生",
        "宇树科技正式启动科创板网上、网下申购，发行价150.80元每股，发行后市值约609.93亿元，发行市盈率219倍。"
        "公司是极少数在IPO前实现规模化盈利的全球人形机器人企业之一，"
        "本次募集资金将投向新一代人形机器人本体研发与产线建设，加快在工业与商用场景的交付节奏。",
        "IT之家", "https://www.ithome.com/c2"),
    _mk("capital", "Stripe 75亿美元收购OpenRouter",
        "Stripe 宣布以75亿美元收购 OpenRouter，为今年 AI 基础设施领域规模最大的并购交易。"
        "OpenRouter 主营多模型统一调用网关，聚合了数十家厂商的模型接口，收购后其路由与计量能力将并入 Stripe 的支付与计费体系，"
        "有望形成按调用量计费的一体化方案。",
        "TechCrunch", "https://techcrunch.com/d1"),
    _mk("capital", "Anthropic完成130亿美元新融资，估值达1830亿美元",
        "Anthropic 完成130亿美元新一轮融资，投后估值达1830亿美元，本轮由多家主权基金领投。"
        "资金将主要用于扩充算力与加强安全研究，公司同时披露企业客户数量与年度经常性收入均较上一年度显著增长，"
        "成为全球估值最高的AI模型公司之一。",
        "TechCrunch", "https://techcrunch.com/d2"),
    _mk("capital", "国内具身智能创企完成B轮，募集8亿元估值60亿元",
        "该创企完成B轮融资，募集8亿元，投后估值60亿元，资金将主要用于人形机器人量产产线建设与核心零部件自研。"
        "公司称其新一代机型已进入小批量交付阶段，本轮融资将支撑产能爬坡，并加快在工业与商用服务场景的验证。",
        "量子位", "https://www.qbitai.com/d3"),
    _mk("tech", "数据中心液冷规模化落地，PUE降至1.08节电25%",
        "该企业在12个数据中心完成液冷方案部署，PUE降至1.08，整体节电25%。"
        "方案采用冷板与浸没两条技术路线并行，适配高功率密度机柜，并配套余热回收系统。"
        "随着单机柜功率持续攀升，液冷正从试点走向规模化，成为新建智算中心的主流选择。",
        "The Verge", "https://www.theverge.com/e1"),
]
LONG_ITEMS = [i for i in LONG_ITEMS if i["url"] in VALID]

ROUNDS = []          # 捕获每轮 prompt 与返回


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    prompt = json.loads(req.data.decode("utf-8"))["messages"][0]["content"]
    idx = len(ROUNDS)
    ROUNDS.append({"prompt": prompt})
    items = SHORT_ITEMS if idx == 0 else LONG_ITEMS
    body = {"summary": "今日AI产业要闻：政策落地、芯片与模型进展、大额并购与具身智能融资。",
            "items": items}
    content = json.dumps(body, ensure_ascii=False)
    return _Resp({"choices": [{"message": {"content": content}}]})


def main():
    np.urllib.request.urlopen = fake_urlopen
    np.call_glm.__globals__["urllib"] = np.urllib

    ok = True

    # ===== 第 1 轮 =====
    s1, items1 = np.generate_news(MATERIAL, "9月14日 星期一", attempt=0)
    print(f"第1轮（短文案）→ 通过过滤 {len(items1)} 条")
    assert len(items1) == 0, f"第1轮本应全被丢弃，实际留下 {len(items1)} 条"
    print("  ✅ 短文案被字数硬校验全部拦下")

    # ===== 第 2 轮（重试）=====
    s2, items2 = np.generate_news(MATERIAL, "9月14日 星期一", attempt=1)

    # B. 重试 prompt 必须含新增修正提示
    p1 = ROUNDS[1]["prompt"]
    has_fix = "字数不达标" in p1 and "正文不足 80 字" in p1
    print(f"  {'✅' if has_fix else '❌'} 重试 prompt 含“字数不达标”修正提示（本次修复的核心）")
    ok &= has_fix
    p0 = ROUNDS[0]["prompt"]
    print(f"  {'✅' if '字数不达标' not in p0 else '❌'} 首轮 prompt 不含该提示（避免误导）")
    ok &= ("字数不达标" not in p0)

    # C. 最终质量
    n = len(items2)
    tl = [len(i["title"]) for i in items2]
    dl = [len(i["desc"]) for i in items2]
    dist = {}
    for i in items2:
        dist[i["cat"]] = dist.get(i["cat"], 0) + 1
    print(f"\n第2轮（修正后）→ {n} 条 / 目标 8")
    print(f"  标题字数 平均 {sum(tl)/len(tl):.1f}｜区间 {min(tl)}-{max(tl)}（8月基准 25.0）")
    print(f"  正文字数 平均 {sum(dl)/len(dl):.1f}｜区间 {min(dl)}-{max(dl)}（8月基准 118.9）")
    print(f"  四类分布 {dist}")

    checks = [
        ("产出 8 条", n == 8),
        ("标题均 ≥15 字", min(tl) >= 15),
        ("正文均 ≥80 字", min(dl) >= 80),
        ("正文均 ≤400 字", max(dl) <= 400),
        ("四类齐全", set(dist) == {"policy", "tech", "industry", "capital"}),
        ("每类 ≥1 条", all(dist[c] >= 1 for c in ("policy", "tech", "industry", "capital"))),
    ]
    print()
    for name, good in checks:
        print(f"  {'✅' if good else '❌'} {name}")
        ok &= good

    # 无编造 URL
    bad_url = [i for i in items2 if i["url"] not in VALID]
    print(f"  {'✅' if not bad_url else '❌'} 无编造 URL")
    ok &= not bad_url

    print("\n" + ("🎉 mock 演练全部通过" if ok else "⚠ 存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
