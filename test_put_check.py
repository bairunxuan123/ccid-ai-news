#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 产业动态每日自动流水线（云端版，由 GitHub Actions 每天 09:30 / 14:30 触发）

职责：
  1. 抓取多个公开新闻源（RSS）最近 72h 的 AI 相关条目 + 中国政府网政策文件库
  2. 调用智谱 GLM API，把素材整理成"国内外各 8 条、四类均衡"的产业动态
     （国内 8 条 = 政策发布 / 技术突破 / 产业动态 / 投融资 各 2 条；
       国外 8 条同理。2026-09-21 用户要求拆成国内、国外两个板块）
  3. 若当天日期尚未写入，则插入 ai-chain-map.html（完整版）与
     ai-chain-map-lite.html（阉割版）的 NEWS_DATA 头部
  4. node 校验两个文件 JS 语法，通过则提交上线（由 workflow 完成 push）

安全性设计：
  - URL 强制白名单：脚本只接受素材源里真实存在的链接，防止模型编造
  - 英文双引号清洗：所有写入 JS 的字符串内部引号一律转中文引号，避免语法错误
  - 幂等：当天已有日期则跳过写入（多次触发无副作用）
  - 失败不破坏线上：任一步骤异常即退出非零，workflow 不提交

环境变量：
  ZHIPU_API_KEY  智谱开放平台 API Key（bigmodel.cn 免费注册，glm-4-flash 免费）
"""
import html as _html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
FULL_HTML = os.path.join(WORKSPACE, "ai-chain-map.html")       # 完整版
LITE_HTML = os.path.join(WORKSPACE, "ai-chain-map-lite.html")  # 阉割版（客户版）

ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_MODEL = "glm-4-flash"   # 免费模型

# 新闻源（GitHub Actions 在美国节点运行，尽量用可达性好的源；单源失败不影响整体）
# [2026-09-14] 扩充：原 6 源里中文源只有 IT之家（综合 IT 站，消费电子噪声大），
#   高质量 AI 垂直源仅 4 个且多在 48h 窗口无更新，导致凑不出 8 条四类均衡。
#   实测补充以下源（可用性已验证）：
#     量子位（中文 AI 垂直，更新到当天）｜AI News（AI 垂直）｜
#     Ars Technica｜IEEE Spectrum AI
RSS_SOURCES = [
    "https://www.ithome.com/rss/",                                  # 中文 IT 综合
    "https://www.qbitai.com/feed",                                  # 量子位（中文 AI 垂直）★ 新增
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",  # Atom
    "https://venturebeat.com/category/ai/feed/",
    "https://www.artificialintelligence-news.com/feed/",            # AI News ★ 新增
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss",  # IEEE Spectrum AI ★ 新增
    "https://feeds.arstechnica.com/arstechnica/technology-lab",     # Ars Technica ★ 新增
    "https://openai.com/news/rss.xml",
]

# 源域名 → 媒体简称。8 月定版里 source 字段填的是"IT之家""The Verge"这类简称，
# 直接告知模型可免去它从 URL 反推媒体名（反推偶尔会写成域名或写错）。
SOURCE_NAMES = {
    "ithome.com": "IT之家",
    "qbitai.com": "量子位",
    "techcrunch.com": "TechCrunch",
    "theverge.com": "The Verge",
    "venturebeat.com": "VentureBeat",
    "artificialintelligence-news.com": "AI News",
    "technologyreview.com": "MIT Tech Review",
    "spectrum.ieee.org": "IEEE Spectrum",
    "arstechnica.com": "Ars Technica",
    "openai.com": "OpenAI",
}


def source_name(url):
    """把 RSS 源地址转成媒体简称，未知源退化为域名。"""
    host = url.split("/")[2] if "//" in url else url
    for dom, name in SOURCE_NAMES.items():
        if dom in host:
            return name
    return host


# 单源取样上限：避免综合源（IT之家单源 60 条）独占素材池，
# 保证 AI 垂直源（TechCrunch/The Verge/MIT/OpenAI）都能进入 prompt
PER_SOURCE_CAP = 30
# 送进 prompt 的素材条数上限。
# [2026-09-14] 120 → 60：素材现在带正文摘要（每条最多 250 字），
# 60 条 × 约 340 字符 ≈ 2 万字符，在模型上下文内可从容容纳；
# 继续用 120 条会把摘要挤掉，等于白抓。
PROMPT_MATERIAL_CAP = 60
# 每条素材的摘要送进 prompt 的字数上限
SUMMARY_IN_PROMPT = 250

# —— 地域维度（2026-09-21 用户要求）——
# 用户原话："每日产业动态按照国内和国外分开（重新设计一下页面，国内国外两个板块，
#   国内四个维度8条动态，国外四个维度8条动态）"
# 于是每天的目标从"8 条（四类各 2）"扩大到"16 条（2 地域 × 4 类，每格 2 条）"。
# 阶段 A 同步改为**按地域分两次选题**：先把素材池按 region_of() 劈成国内/国外，
# 再各出一份候选。这样模型不会因为素材里国内稿子多，
# 就顺手把国外名额也填成国内新闻。
REGIONS = ["cn", "intl"]
REGION_LABELS = {"cn": "国内", "intl": "国外"}
# 每个地域（含四个维度）最终要写入的条数
PER_REGION_ITEMS = 8
# 最终写入的条目数：国内 8 + 国外 8
MAX_ITEMS = PER_REGION_ITEMS * len(REGIONS)
# 每个"地域 × 维度"格子最终至少要有几条（8 格 × 2 = 16 条）
MIN_PER_CELL = 2

# 要求模型输出的候选条数（**每个地域**）。8 格 × 4 = 32 条，分两次要。
# [2026-09-21] 由单次 14 条改为"每地域 16 条 × 2 轮地域"。原因见上面的地域说明：
# 一次要 32 条既容易输出截断，也无法保证两个地域都被照顾到。
CANDIDATE_ITEMS = 16
# 阶段 A 每个"地域 × 维度"格至少要有多少条候选，低于此值就补选一轮并合并。
# [2026-09-14] 真实 API 复验（run 34819377914）产出 8 条、标题均 25.5 字、
# 正文均 134.2 字，长度全部达标，但四类分布是
# {政策发布:3, 技术突破:2, 投融资:3}，"产业动态"一条都没有 —— 根因是
# 阶段 A 给的 industry 候选本就少，再被阶段 B 的硬门槛筛掉几条，
# select_balanced 最终无米下锅（它只能从候选里挑，变不出没有的类别）。
# 用户要求按地域×四类各 2 条，所以候选阶段就必须按格卡下限。
MIN_CANDS_PER_CELL = 3
# 阶段 A 最多跑几轮（首轮 + 补选轮）
MAX_SELECT_ROUNDS = 3
# 阶段 B 最多写多少条合格正文就停手（上限；
# 提前收工条件是"8 格每格够 2 条"，即 16 条）
DESC_TARGET = 26
# 阶段 B 每格至少要有多少条合格正文，8 格都够了才允许提前收工
MIN_PER_CELL_DESC = 2

# —— 8 月标准（用户要求"以后都按 8 月标准推送"）——
# 8 月实测：236 条的平均字数 —— 标题 25.0 字、正文 118.9 字，正文最短 71 字、
# 无一低于 60 字。9 月退化到标题 16.3 字、正文 75.3 字且 43% 不足 60 字。
# 标题上限：8 月实测中位数 24 字，故 40 字以上视为失控（实测出现过 52 字）。
MIN_TITLE_LEN = 15
MAX_TITLE_LEN = 40
MIN_DESC_LEN = 80

# AI 相关性过滤（强命中 / 弱命中+排除词，英文按词边界，避免 email/said 误伤）
STRONG_EN = [
    "openai", "anthropic", "chatgpt", "gemini", "claude", "llama", "deepseek",
    "mistral", "nvidia", "copilot", "llm", "hbm", "token", "machine learning",
    "neural", "transformer", "generative ai", "robotaxi", "agentic",
    "grok", "xai", "qwen", "midjourney", "stable diffusion", "hugging face",
    "artificial intelligence", "foundation model", "gpu", "datacenter",
    # 2026-09-14 补：OpenAI/主流模型系专有名词，此前缺失导致
    # "Perplexity trusts GPT-6 Astra" 这类高价值素材被 is_ai_related 误拦
    "perplexity", "gpt", "codex", "sora", "dall-e", "o1", "o3",
    "hyperclova", "phi-", "granite", "command r",
]
STRONG_CN = [
    "人工智能", "大模型", "大语言模型", "智能体", "多模态", "自动驾驶", "智驾",
    "算力", "数据中心", "英伟达", "深度学习", "机器学习", "神经网络", "生成式",
    "大模型公司", "ai大模型", "具身智能", "智算", "aigc", "ai服务器", "ai芯片",
    "ai应用", "ai安全", "ai治理", "ai眼镜", "ai手机", "ai pc",
    # 2026-09-14 补：国内 AI 厂商/模型名（中文源的 AI 新闻多以公司名+模型名出现）
    "豆包", "通义", "文心", "混元", "kimi", "月之暗面", "智谱", "百川",
    "阶跃", "minimax", "零一万物", "商汤", "科大讯飞", "讯飞星火", "昆仑万维",
    "盘古", "昇腾", "寒武纪", "摩尔线程", "地平线", "小马智行", "文远知行",
    "宇树", "优必选", "智元机器人",
]
WEAK_CN = ["芯片", "机器人", "gpu", "智能"]
# 兜底放宽时使用的"AI 邻域"词：只有命中这些才允许把弱相关新闻纳入
ADJACENT_CN = [
    "芯片", "半导体", "算力", "数据中心", "云服务", "机器人", "自动驾驶",
    "智能驾驶", "语音", "模型", "算法", "具身", "数据库", "数字人", "视觉识别",
]
BLOCK_CN = [
    # 纯消费品（无 AI 技术内容）
    "相机", "镜头", "电视", "戒指", "键盘", "鼠标", "爆料", "评测",
    "手办", "盲盒", "玩偶",
    # 汽车新品与传闻（无 AI 技术内容）
    "官图", "试驾", "新车", "mpv", "suv", "座舱", "灯效", "rgb", "影音",
    "续航", "渲染图", "内饰",
    # 智能家居 / 众筹类硬件
    "众筹", "晾衣", "灯板", "英寸", "按摩仪", "空气净化", "除湿",
    # 产品预告（无实质内容）
    "预告", "即将发布", "新品发布",
    # 操作系统与终端系统更新（非 AI 产业事件；精确匹配系统更新场景）
    "win11 系统", "win10 系统", "windows 11 系统", "windows 10 系统",
    "ios1 系统", "ios2 系统", "ipados 系统",
    "macos 系统", "android 系统", "安卓系统", "鸿蒙系统",
    # 政治人物 / 党务 / 选举（与产业技术无关，不适合收录）
    "奥巴马", "特朗普", "拜登", "哈里斯", "民主党", "共和党", "白宫", "总统",
    "大选", "选举", "国会", "参议院", "众议院", "议员", "首相", "内阁",
    "obama", "trump", "biden", "harris", "democrat", "republican",
    "congress", "senate", "white house", "parliament",
    # 通用 AI 助手 / 智能体名字的纯"AI 应用介绍"（无产业事件）
    # 注：与"AI 算力"、"AI 芯片"等产业词不冲突；只拦截明确是消费产品的
    "ai 玩具", "ai 戒指", "ai 眼镜",
]

# TITLE_BLOCK 用于"二次过滤"：标题含聚合类/某些消费品但话题可能涉及 AI 的
# [2026-09-14] 扩充：素材池实测发现以下漏网消费电子会进 prompt 并被模型选中
#   —— 努比亚/摩托罗拉/realme 新机、小米平板、米家窗帘、奇瑞捷豹路虎新车、
#      Win11 Copilot、ANKER AI 会议耳机等。这些即使带"AI"字样也不属于 AI 产业动态。
#   设计：消费电子终端与系统更新【无条件拦】；产品发布向动词【仅当标题不含
#   AI 硬件信号时拦】——这样"英伟达上架 RTX PRO 专业显卡"能保留，
#   而"realme 手机预热"被拦。汽车不用品牌名拦（小鹏/特斯拉同时是智驾/机器人重要厂商），
#   改用"万元起"这类零售价格特征精准识别新车新闻。
TITLE_BLOCK = [
    # 聚合汇总类
    "早报", "日报", "晚报", "周报", "月报", "盘点", "汇总", "速览", "一周要闻",
    # 消费电子终端（含"AI 手机/AI 耳机"这类营销话术）
    "手机", "平板", "笔记本", "耳机", "手表", "手环", "充电宝", "移动电源",
    # 智能家居 / 家电
    "窗帘", "米家", "空气炸锅", "电饭煲", "空调", "冰箱", "洗衣机",
    "扫拖", "扫地机器人", "门锁", "浴霸", "晾衣机", "加湿器",
    # 操作系统与终端更新（放宽为直接匹配，此前精确匹配导致 Win11 Copilot 漏网）
    "win11", "win10", "windows 11", "windows 10", "ipados", "macos",
    "ios", "iphone", "ipad", "macbook", "airpods", "apple watch", "鸿蒙",
    # 汽车新品（非 AI 产业事件）
    "官图", "试驾", "新车", "座舱", "续航", "渲染图",
    # 操作系统内核（Linux 内核发布等，借 "AMDGPU"/"GPU" 关键词混入）
    "内核",
]

# 产品发布向动词：仅当标题【不含】AI 硬件信号时才拦截（避免误杀算力硬件新闻）
TITLE_BLOCK_SOFT = [
    "新机", "预热", "开售", "预售", "上架", "首销", "万元起",
    "时事", "寻求胜利",
]
# AI 硬件/算力信号：命中则豁免 TITLE_BLOCK_SOFT
_AI_HW_SIGNAL = [
    "算力", "数据中心", "gpu", "显卡", "显存", "服务器", "推理", "训练",
    "英伟达", "nvidia", "amd", "intel", "海思", "昇腾", "寒武纪", "摩尔线程",
    "大模型", "人工智能", "智算",
]

# 匹配前先去掉空格/连字符等分隔符：使 "iOS 27" 能命中 "ios2"、"Win 11" 命中 "win11"
_BLOCK_NS = [re.sub(r"[\s\-_·]+", "", b) for b in BLOCK_CN]


def hard_blocked(title):
    """硬性排除：消费电子、汽车新品、产品预告、系统更新、政治人物等"""
    t = re.sub(r"[\s\-_·]+", "", (title or "").lower())
    return any(b in t for b in _BLOCK_NS)


def is_ai_related(title):
    """标题是否为 AI 相关。

    先做硬性排除（消费电子/汽车新品/产品预告/系统更新/政治人物），再做相关性判定。
    英文 ai 用前后非字母判定（避免 "email"/"said" 误伤，也兼容 "AI行业" 这类中英混排）。
    """
    t = title.lower()
    if hard_blocked(title):
        return False
    if re.search(r"(?<![a-z])ai(?![a-z])", t):
        return True
    if any(k in t for k in STRONG_EN):
        return True
    if any(k in t for k in STRONG_CN):
        return True
    if any(k in t for k in WEAK_CN):
        return True
    return False


def is_ai_adjacent(title):
    """AI 邻域（弱相关）：用于素材不足时的兜底，比 is_ai_related 宽松但仍有明确口径。"""
    t = title.lower()
    if is_ai_related(title):
        return True
    if hard_blocked(title):
        return False
    return any(k in t for k in ADJACENT_CN)


# 易失真的数字表达：金额 / 估值 / 百分比 / 增长倍数
RISKY_NUM_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:亿|万|千))*\s*(?:美元|美金|元|人民币|港币|倍|%)"
)


def risky_number_phrases(text):
    """抽取文本中的金额/百分比/倍数表达（去空格归一化，仅用于日志展示）"""
    if not text:
        return set()
    return {re.sub(r"\s+", "", m.group(0)) for m in RISKY_NUM_RE.finditer(text)}


# 英文金额简写：$500M / 500 million / 1.5 billion / $2B
_EN_MONEY_RE = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*(thousand|million|billion|bn|k|m|b)(?![a-zA-Z])", re.I
)
_MAG = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
        "b": 1e9, "bn": 1e9, "billion": 1e9}


def risky_tokens(text, bare_numbers=False):
    """把金额/百分比/倍数统一换算成可跨中英文比对的规范 token。

    素材多为英文（"$500M"），生成结果是中文（"5亿美元"），
    直接做子串匹配会误杀，因此统一折算为绝对数值再比对。
    同时对金额额外发放 "mag:" 别名，使 "$2B" 能匹配中文的"20亿"（未带币种）。

    bare_numbers=True 时额外收录裸数字（≥4 位含千分位，如 "50,000"），
    仅用于素材侧：中文表述"出货5万台"折算为 50000，需要与英文摘要里的
    "50,000 units" 对上。文本侧不可开启——否则"2026年"这类年份也会变成
    必须溯源的硬约束，素材中稍缺提及就整条误杀。
    """
    if not text:
        return set()
    raw = text
    s = re.sub(r"\s+", "", text)
    toks = set()

    if bare_numbers:
        for m in re.finditer(r"\d[\d,]{3,}", s):
            try:
                toks.add("mag:%d" % round(float(m.group(0).replace(",", ""))))
            except ValueError:
                pass

    def money(n):
        toks.add("amt:%d" % round(n))
        toks.add("mag:%d" % round(n))

    # 中文口径金额：5亿 / 5亿美元 / 500万美元 / 2000万元
    # 支持双量级（"5千万美元" = 5 × 千 × 万 = 5000万美元），
    # 与英文 "$50 million" 折算后同为 5e7，可正确比对。
    for m in re.finditer(r"(\d+(?:\.\d+)?)([亿万千]?)([亿万千]?)(美元|美金|元|人民币|港币)", s):
        num = float(m.group(1))
        mult = 1.0
        for g in (m.group(2), m.group(3)):
            if g:
                mult *= {"亿": 1e8, "万": 1e4, "千": 1e3}[g]
        money(num * mult)
    # 中文裸量级（无币种）：20亿 / 500万 / 5千万
    # 负向断言同时排除后接量级字的情形，否则"5千万美元"会被切出
    # 一个虚假的"5千"token，导致比对失败。
    for m in re.finditer(r"(\d+(?:\.\d+)?)([亿万千])([亿万千])?(?![美元人民币港亿万千])", s):
        mult = {"亿": 1e8, "万": 1e4, "千": 1e3}[m.group(2)]
        if m.group(3):
            mult *= {"亿": 1e8, "万": 1e4, "千": 1e3}[m.group(3)]
        toks.add("mag:%d" % round(float(m.group(1)) * mult))
    # 英文口径金额：$500M / 2 billion / 500M
    # 必须在原文本（保留空格）上匹配：去空格会把 "M valuation" 粘成 "Mvaluation"，
    # 词边界失效导致英文金额整体漏检。
    for m in re.finditer(_EN_MONEY_RE.pattern, raw, re.I):
        num = float(m.group(1))
        money(num * _MAG[m.group(2).lower()])
    # 百分比
    for m in re.finditer(r"(\d+(?:\.\d+)?)%", s):
        toks.add("pct:%g" % float(m.group(1)))
    # 倍数（中文"3倍" / 英文"3x"、"3 times" 统一折算）
    # [2026-09-14] 补英文口径：此前只认中文"倍"，英文源素材里的 "3x"
    # 被判为素材中不存在，导致"吞吐提升3倍"这类忠实于摘要的表述整条被丢弃。
    for m in re.finditer(r"(\d+(?:\.\d+)?)倍", s):
        toks.add("x:%g" % float(m.group(1)))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:x|times)(?![a-zA-Z])", raw, re.I):
        toks.add("x:%g" % float(m.group(1)))
    return toks


def ungrounded_numbers(text, material_text):
    """返回文本中【无法在素材里核实】的金额/百分比/倍数 token（空集=全部可核实）。

    [2026-09-14] 与 numbers_grounded 的区别：这里把"差集"暴露出来，
    好让上层知道具体是哪个数字对不上（amt:300000000 / pct:40 / x:3 …），
    从而在重写提示里指名道姓地要求改掉，而不是整条丢掉。
    实测依据（run 34818610548）：10 条候选里有 4 条因数字问题被直接丢弃，
    而这些数字多数是模型顺手四舍五入或补了一句"提升40%"造成的——
    重写一次即可修正，丢弃等于白扔一条可用新闻。
    """
    toks = risky_tokens(text)
    if not toks:
        return set()
    return toks - risky_tokens(material_text, bare_numbers=True)


def numbers_grounded(text, material_text):
    """文本里的金额/百分比/倍数必须在素材中真实出现过，否则视为不可核实。

    素材只有标题、没有正文，LLM 一旦"推算"金额或估值就会失真（例如
    "估值约2万亿美元"），因此这类数字必须能被素材标题支持。
    比对时统一折算口径，避免 "$500M" 与 "5亿美元" 被误判为不匹配。
    """
    return not ungrounded_numbers(text, material_text)


def _en_word_count(text):
    """文本里非白名单英文词的数量（粗口径，用于判断标题是否整句英文）。"""
    words = re.findall(r"[A-Za-z][A-Za-z\.\-]*", text or "")
    return sum(1 for w in words if w.lower().strip(".") not in _EN_ALLOW)


def title_english_residue(title):
    """标题是否为未翻译的英文原句。

    [2026-09-14] 新增。实测（run 34818610548）出现
    "Rapidly scaling online storage" 这种标题——模型把英文原标题直接交了，
    没有翻译。这类候选应当在阶段 A 就筛掉，别浪费阶段 B 的调用。
    判据：非白名单英文词 ≥3 个，且中文字符占比不足三成。
    """
    t = title or ""
    cn = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
    if cn >= 6:
        return False
    return _en_word_count(t) >= 3


# 允许在中文文本中直接出现的英文专有名词 / 技术缩写
_EN_ALLOW = {
    "openai", "anthropic", "chatgpt", "gpt", "nvidia", "microsoft", "windows",
    "google", "alphabet", "deepmind", "gemini", "meta", "facebook", "apple",
    "amazon", "aws", "intel", "amd", "qualcomm", "arm", "tesla", "spacex",
    "xai", "grok", "claude", "llama", "mistral", "deepseek", "kimi", "moonshot",
    "qwen", "doubao", "baidu", "alibaba", "tencent", "huawei", "bytedance",
    "musk", "altman", "huang", "siri", "copilot", "office", "cloudflare",
    "sequoia", "venturebeat", "techcrunch", "verge", "wired", "reuters",
    "gpu", "cpu", "tpu", "npu", "llm", "api", "sdk", "ipo", "ceo", "cto",
    "hbm", "ipo", "rag", "agc", "aigc", "saas", "tiktok", "youtube", "x",
    "ios", "macos", "iphone", "ipad", "macbook", "android", "linux", "python",
}


def stray_english_count(text):
    """统计文本中"不该残留的英文"。

    [2026-09-14 修复] 原实现把所有非白名单英文词（长度≥4）都计数，阈值 3。
    但真实新闻标题里本来就会嵌公司名/人名（Nscale、Fidji Simo、Mecka…），
    于是 "Nscale添加前OpenAI高管Fidji Simo" 被判为"英文未翻译"整条丢弃
    —— 实测每天因此损失 1 条，直接拖累 8 条目标。
    改为只认【真正的英文残留】：
      · 连续 ≥3 个非白名单英文词（说明是一句英文短语/句子），或
      · 非白名单英文词总数 ≥6（长段英文）
    零散嵌在中文里的专有名词不再计分。

    [2026-09-21 修复"连续"的口径] 原实现只看英文词序列，中间的中文被无视，
    于是 "Stripe宣布…收购OpenRouter，…将并入Stripe的支付体系" 这种
    **同一公司名出现 3 次、每次都隔着中文**的正文会被当成"连续英文短语"误杀。
    现在把中文也编进 token 序列：两个英文词之间只要隔了汉字就视为不连续。
    "总数 ≥6"那条同理改为**去重后**计数，避免同一专名反复出现而误报。
    """
    if not text:
        return 0
    toks = re.findall(r"[A-Za-z][A-Za-z\.\-]*|[\u4e00-\u9fff]", text)
    bad = []
    run = 0
    for tok in toks:
        if not tok[0].isascii():        # 汉字：打断"连续英文"
            run = 0
            continue
        w = tok.lower().strip(".")
        if w in _EN_ALLOW:
            run = 0
            continue
        bad.append(w)
        if len(tok) >= 4:
            run += 1
            if run >= 3:                # 真·连续英文短语/句子
                return len(bad)
        else:
            run = 0
    if len(set(bad)) >= 6:              # 长段英文（去重后仍很多）
        return len(bad)
    return 0


# 豁免词：命中则认为不是消费电子硬件（"手机助手"是 AI 应用形态，非终端硬件）
_TITLE_EXEMPT = [
    "手机助手", "手机智能体", "手机端ai", "端侧ai", "移动端ai助手",
]


def title_blocked(title):
    """标题是否为应当排除的类型（聚合汇总 / 消费电子 / 汽车新品 / 系统更新）。

    [2026-09-14] 由"单一 has-any 匹配"升级为两级判定：
      · TITLE_BLOCK      —— 消费电子终端、系统更新、汽车新品、聚合类：无条件排除
      · TITLE_BLOCK_SOFT —— 产品发布向动词（新机/预售/上架/万元起…）：
                            仅当标题【不含】AI 硬件信号时才排除，
                            避免误杀"英伟达上架 RTX PRO 专业显卡"这类算力硬件新闻
    匹配前统一去掉空格/连字符，覆盖 "Win 11"、"iOS 27" 等带空格写法。
    """
    t = re.sub(r"[\s\-_·]+", "", (title or "").lower())
    if any(e in t for e in _TITLE_EXEMPT):
        return False
    if any(b in t for b in TITLE_BLOCK):
        return True
    if any(b in t for b in TITLE_BLOCK_SOFT) and not any(s in t for s in _AI_HW_SIGNAL):
        return True
    return False

CAT_LABELS = {
    "policy": "政策发布",
    "tech": "技术突破",
    "industry": "产业动态",
    "capital": "投融资",
}

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[pipeline] {msg}", flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _strip_html(s, limit=600):
    """把 RSS 摘要里的 HTML 片段洗成纯文本（去标签、解实体、压空白、截断）。

    [2026-09-14] 与"恢复 8 月标准"配套新增。RSS 的 description / content 字段
    常是带标签的 HTML（也可能整体被 entity 编码过一次），需要解两遍实体。
    """
    if not s:
        return ""
    try:
        s = _html.unescape(s)                       # 先解一层（&lt;p&gt; 形态）
        s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
        s = re.sub(r"<[^>]+>", " ", s)              # 去标签
        s = _html.unescape(s)                       # 再解一层（残留实体）
        s = re.sub(r"\s+", " ", s).strip()
    except Exception:
        return ""
    return s[:limit]


# RSS/Atom 中承载正文摘要的字段名（各源叫法不同，取最长的那个）
_SUM_TAGS = ("description", "summary", "content", "encoded", "subtitle")


def fetch_rss_items(source):
    """抓取单个 RSS/Atom 源，返回 [{title, url, source, published, summary}]

    [2026-09-14] 新增 summary（正文摘要）字段。此前只取标题，导致模型手里
    没有任何事实细节，只能写出"某公司面临挑战"这类空泛标题和十几字的正文；
    而 8 月那批高质量条目（含发行价、市盈率、融资额等具体数字）正是
    依赖 RSS 自带的正文摘要写出来的。所有源均提供该字段，不应丢弃。
    """
    try:
        raw = http_get(source)
        # 剔除 XML 非法控制字符（部分源含 \x00-\x1f 导致解析失败）
        raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
        root = ET.fromstring(raw)
    except Exception as e:
        log(f"  RSS 抓取失败 {source}: {e}")
        return []

    def local(name):
        return name.split("}")[-1]

    def first_text(el, name):
        for c in el.iter():
            if local(c.tag) == name and c.text and c.text.strip():
                return c.text.strip()
        return ""

    def first_link(el):
        for c in el.iter():
            if local(c.tag) == "link":
                href = c.get("href")
                if href:
                    return href.strip()
                if c.text and c.text.strip():
                    return c.text.strip()
        return ""

    def first_summary(el):
        """取该条目所有候选字段中最长的正文摘要（content 通常比 summary 全）"""
        best = ""
        for c in el.iter():
            if local(c.tag) in _SUM_TAGS and c.text and c.text.strip():
                t = _strip_html(c.text)
                if len(t) > len(best):
                    best = t
        return best

    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = first_text(node, "title")
        link = first_link(node)
        pub = first_text(node, "pubDate") or first_text(node, "published") or first_text(node, "updated")
        if not title or not link:
            continue
        items.append({
            "title": title,
            "url": link,
            "source": source_name(source),
            "published": pub,
            "summary": first_summary(node),
        })
    return items


def _parse_pubdate(s):
    """解析 RSS 发布时间（RFC822 / ISO8601），失败返回 None（不因此丢弃素材）。"""
    if not s:
        return None
    s = s.strip()
    try:                                    # "Mon, 14 Sep 2026 00:00:00 GMT"
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
    except Exception:
        pass
    try:                                    # "2026-09-14T00:00:00Z"
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 中国政府网「政策文件库」：政策发布的官方来源
#
# [2026-09-18 用户要求] "现在的产业动态，尤其是政策发布，尽量用国内官方政府发的
#   人工智能相关政策，不要随便一条动态都叫政策发布"。
#   此前 policy 类全靠科技媒体转载（IT之家/量子位），媒体标题里带"监管""法案"
#   的国外新闻也会被标成政策发布，甚至出现"某国议员提法案"占掉政策名额。
#   这里直接调中国政府网政策文件库的检索接口取**原始政策文件**：
#     · t=zhengcelibrary_gw  国务院文件（含中办/国办印发的规划、条例）
#     · t=zhengcelibrary_bm  部门文件（工信部/发改委/网信办/国家数据局等）
#   返回字段含 title / url / pubtimeStr / summary / puborg / pcode（发文字号），
#   正文摘要里带文号与开头内容，正好可以写出"工信部印发《…》"这类权威标题。
# ---------------------------------------------------------------------------
GOV_POLICY_SEARCH = "https://sousuo.www.gov.cn/search-gov/data"
# 检索词：覆盖当前 AI 产业政策的主要落点。
# 实测（2026-09-18）中国政府网政策文件库里"标题含 AI 关键词"的文件发布节奏
# 约每月 2-4 份（近 45 天仅 3 份），因此检索词要铺得宽一些，否则经常一无所获。
OFFICIAL_POLICY_KWS = [
    "人工智能", "人工智能+", "算力", "大模型", "智能体", "智能制造",
    "机器人", "智能网联", "数据要素",
]
# 政策文件类型：国务院文件 / 部门文件 / 部委联合发文
OFFICIAL_POLICY_TYPES = ["zhengcelibrary_gw", "zhengcelibrary_bm", "zhengcelibrary_gb"]
# 取最近多少天内的政策。
# [2026-09-18] 7 天太紧（实测最新一份政策在 7 天前，结果全被时间窗滤掉）；
# 放宽到 14 天，并配合"已发布过就不再取"（见 _published_urls）保证同一条政策
# 只上一次简报，不会连日重复。
OFFICIAL_POLICY_DAYS = 14
# 政策原文的摘要里含 HTML 片段，送进 prompt 前先洗净
_GOV_TAG = re.compile(r"<[^>]+>")


def _gov_date(s):
    """把政府网的 "2026.09.11" 解析成 datetime，失败返回 None。"""
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", str(s or "").strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _published_urls():
    """已上线的新闻 URL 集合（完整版+阉割版都扫）。

    官方政策的时间窗口放宽到 7 天后，同一条政策会被连续几天捞到；
    把已经上线的 URL 排除掉，避免"工信部印发XX方案"在简报里连刷一周。
    """
    urls = set()
    for p in (FULL_HTML, LITE_HTML):
        try:
            with open(p, encoding="utf-8") as f:
                urls |= set(re.findall(r'url:\s*"([^"]+)"', f.read()))
        except Exception:
            continue
    return urls


def collect_official_policies(days=OFFICIAL_POLICY_DAYS, before=None):
    """抓取中国政府网政策文件库里 AI 相关政策原文。

    返回素材条目列表（与 RSS 素材同构：title/url/source/published/summary），
    额外带 _official=True 供排序与提示词使用。任一关键词抓取失败都只跳过，
    不影响主流程——官方源只是"更好"，不是"必须有"。

    before：只取该日期（含）之前发布的政策。回填历史日期时必须传，否则会把
    "未来"的政策写进过去的简报里（如重生成 09-10 却收进 09-12 的政策）。
    """
    pub_urls = _published_urls()
    end = datetime.strptime(before, "%Y-%m-%d") if before else datetime.utcnow()
    cutoff = end - timedelta(days=days)
    out, seen_url = [], set()
    for kw in OFFICIAL_POLICY_KWS:
        for t in OFFICIAL_POLICY_TYPES:
            params = urllib.parse.urlencode({
                "t": t, "q": kw, "searchfield": "title",
                "sort": "pubtime", "sortType": "1", "p": "1", "n": "10",
            })
            try:
                raw = http_get(f"{GOV_POLICY_SEARCH}?{params}", timeout=20)
                data = json.loads(raw)
            except Exception as e:
                log(f"  官方政策检索失败（{kw}/{t}）: {e}")
                continue
            for it in ((data.get("searchVO") or {}).get("listVO") or []):
                url = str(it.get("url") or "").strip()
                title = _GOV_TAG.sub("", str(it.get("title") or "")).strip()
                if not url or not title or url in seen_url:
                    continue
                seen_url.add(url)
                if url in pub_urls:
                    continue                      # 这条政策之前已经发布过了
                dt = _gov_date(it.get("pubtimeStr"))
                if dt is None or dt < cutoff or dt > end:
                    continue                      # 窗口外，或（回填时）晚于目标日期
                # 政策库的检索是模糊匹配（"算力"会带出无关文件），
                # 用标题/摘要里的 AI 关键词再确认一次，宁少勿滥。
                blob = title + " " + _GOV_TAG.sub("", str(it.get("summary") or ""))
                if not any(k in blob for k in AI_POLICY_TOPICS):
                    continue
                out.append({
                    "title": title,
                    "url": url,
                    "source": "中国政府网",
                    "published": dt.strftime("%Y-%m-%d"),
                    "summary": _strip_html(str(it.get("summary") or ""), limit=400),
                    "org": str(it.get("puborg") or "").strip(),
                    "docno": str(it.get("pcode") or "").strip(),
                    "_official": True,
                })
    # 同一份文件可能被多个关键词命中，按 URL 去重后再按时间倒序
    uniq, got = {}, set()
    for it in out:
        if it["url"] in got:
            continue
        got.add(it["url"])
        uniq[it["url"]] = it
    res = sorted(uniq.values(), key=lambda x: x["published"], reverse=True)
    if res:
        log(f"官方政策素材：{len(res)} 条（近 {days} 天，来源：中国政府网政策文件库）")
        for it in res[:6]:
            log(f"    · {it['published']} {it['title'][:38]}")
    else:
        log(f"官方政策素材：0 条（近 {days} 天）")
    return res


def collect_material(hours=72):
    """汇总所有源最近 N 小时且 AI 相关的条目，去重；**按源均衡取样**。

    [2026-09-14 关键修复] 原实现为
        for src in RSS_SOURCES:
            for it in fetch_rss_items(src): ...
            if len(material) >= 60: break
    而 IT之家（第一个源）单源就返回 60 条，循环在第一个源之后直接 break，
    TechCrunch / The Verge / MIT Tech Review / OpenAI 四个高质量 AI 垂直源的
    素材全部丢失 —— LLM 只能看到 IT之家的消费电子，于是产出"智能门锁+浴霸+扫拖"。
    同时 cutoff 变量算了却从未参与过滤（OpenAI RSS 含 1192 条历史文章，必须靠它滤掉）。

    现改为：遍历全部源、每源最多 PER_SOURCE_CAP 条、cutoff 真正生效。
    """
    seen, material = set(), []
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    dist = []

    # —— 官方政策优先入池（2026-09-18）——
    # 用户要求"政策发布尽量用国内官方政府发的AI相关政策"。搜索结果里 policy 类
    # 最稀缺（媒体源未必天天有政策稿），所以先把中国政府网政策文件库里的原文
    # 放到 material 最前面：下面的按源轮转重排会把首个 bucket 排在最前，
    # 于是这些官方文件必然落在 prompt 前部，模型不会漏看。
    # 它们已在 collect_official_policies 里做过时间窗口与 AI 相关性过滤，
    # 因此这里**不再套 72h 窗口**（政策发布间隔常超过 3 天）。
    official = collect_official_policies()
    for it in official:
        if it["url"] in seen or title_blocked(it["title"]):
            continue
        seen.add(it["url"])
        material.append(it)

    for src in RSS_SOURCES:
        got, total = 0, 0
        for it in fetch_rss_items(src):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            total += 1
            pub = _parse_pubdate(it.get("published"))
            if pub is not None and pub < cutoff:
                continue                    # 超出时间窗口的历史文章
            if not is_ai_related(it["title"]):
                continue
            if title_blocked(it["title"]):
                continue                    # 消费电子/系统更新/汽车新品不进素材池
            material.append(it)
            got += 1
            if got >= PER_SOURCE_CAP:
                break
        dist.append(f"{src.split('/')[2]}={got}/{total}")
    # 按源轮转交错重排：prompt 只截取前 N 条，若不重排则列表前部会被单一综合源占满
    buckets = {}
    for m in material:
        buckets.setdefault(m["source"], []).append(m)
    interleaved, i = [], 0
    while any(len(v) > i for v in buckets.values()):
        for v in buckets.values():
            if len(v) > i:
                interleaved.append(v[i])
        i += 1
    log(f"素材汇总：{len(interleaved)} 条 AI 相关（近 {hours}h）"
        f"，其中官方政策 {len(official)} 条")
    log("  来源分布（采用/原始）：" + "  ".join(dist))
    return interleaved


# ---------------------------------------------------------------------------
# LLM 生成
# ---------------------------------------------------------------------------
def call_glm(prompt, temperature=0.4):
    """调用智谱 GLM 生成。

    [2026-09-14 修复] 原先未指定 max_tokens，沿用 API 默认值（偏小），
    要求输出 8-10 条（每条 title+desc+source+url 约 120-150 token）时
    极易被截断 —— json.loads 整体失败，一次生成完全白费。
    这里显式给足 4096，并把超时从 90s 放宽到 180s（输出变长后耗时增加）。

    [2026-09-14 新增] 限流退避重试。改成两阶段后每天调用次数从 1-2 次升到
    11-20 次（逐条写正文），实测诊断脚本连续调用时会撞上
    `code 1302 您的账户已达到速率限制`。原先遇到即抛异常、整次生成白费，
    现在按 3s/6s/9s 退避重试，最多 4 次；非限流类错误不重试，直接抛出。
    """
    body = json.dumps({
        "model": ZHIPU_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 4096,
    }).encode("utf-8")
    last = None
    for i in range(4):
        req = urllib.request.Request(
            ZHIPU_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ZHIPU_API_KEY}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            last = f"HTTP {e.code} {detail}"
            throttled = (e.code == 429 or "1302" in detail
                         or "速率限制" in detail or "rate" in detail.lower())
            if not throttled:
                raise
            wait = 3 * (i + 1)
            log(f"  触发限流，{wait}s 后重试（第{i+1}/4 次）：{detail[:80]}")
            time.sleep(wait)
        except Exception as e:
            last = str(e)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GLM 调用失败（已重试 4 次）：{last}")


def build_select_prompt(material, today_cn, region, attempt=0, used=None):
    """阶段 A：只做选题与拟标题，不写正文。**按地域分批调用**。

    [2026-09-14] 拆成两阶段的实测依据：glm-4-flash 在"一次输出 10 条完整
    条目（title+desc+source+url）"的任务下，会主动压缩每条正文的输出配额，
    正文平均只有 51 字；而 8 月定版标准是 118.9 字（236 条实测）。
    把"写正文"拆成单条窄任务（见 build_desc_prompt）后模型才写得长。
    阶段 A 输出很短，模型能把注意力放在选题判断与标题打磨上。

    [2026-09-21 按地域分批] 用户要求"国内 8 条 + 国外 8 条"。若仍是一次调用
    要 32 条候选，模型会把注意力集中在素材量大的那一侧（英文源十倍于中文源），
    国外/国内必然有一边凑不满。所以 material 传入的已经是**该地域专属的素材池**
    （见 split_material_by_region），一次只要 16 条、且全属同一地域，
    模型没有"选哪边"的余地。

    used 是前几轮已入选的 (title, url) 列表，用于补选轮——不列出来的话模型会
    把上一轮的原话再抄一遍，补选等于空转（真实复验 run 34821688274：补选轮
    输出的 16 条里 11 条是重复素材，只净增 5 条候选）。
    """
    rlabel = REGION_LABELS[region]
    rname = "中国国内" if region == "cn" else "中国境外（海外）"

    def fmt(i, m):
        sm = (m.get("summary") or "").strip()[:SUMMARY_IN_PROMPT]
        head = f"{i+1}. {m['title']}\n   来源：{m['source']} ｜ URL：{m['url']}"
        return f"{head}\n   摘要：{sm}" if sm else f"{head}\n   摘要：（无，仅可依据标题撰写）"

    lines = "\n".join(
        fmt(i, m) for i, m in enumerate(material[:PROMPT_MATERIAL_CAP])
    )
    if attempt == 0:
        temp_note = ""
    else:
        temp_note = (
            "\n\n【上一轮不合格，本次务必修正】上一轮选题后可用条目不足。"
            "请按以下三点修正：①标题须 20-32 字（低于 15 字、高于 40 字一律作废），"
            "务必写足三要素（主体 + 动作 + 结果）；②**四类都要有候选**——"
            "policy 政策发布、tech 技术突破、industry 产业动态、capital 投融资，"
            "四类各出 4 条，尤其别把“产业动态”漏掉（实测最容易缺的就是这一类）；"
            "③不要选英文原标题直接照抄的素材。"
        )
    if used:
        used_block = (
            "\n\n【以下素材前几轮已经选过，本轮禁止再选】\n"
            + "\n".join(f"- 《{t}》 {u}" for t, u in used[:30])
            + "\n本轮输出的每一条 URL 都必须与上面这些不同，"
              "请去素材表里翻找**还没被选过**的其他新闻；"
              "尤其如果某几类已经积累了不少候选，本轮就把精力放在补齐候选最少的类别上。\n"
        )
    else:
        used_block = ""
    return f"""今天是{today_cn}。下面是抓取的 AI 相关新闻素材，**全部属于「{rlabel}」这一批**（{rname}主体），每条含标题、正文摘要与来源 URL。{temp_note}

素材：
{lines}
{used_block}

请从中挑选当日最有产业价值的 AI 新闻，输出**{rlabel}**选题清单（**只出选题与标题，正文由后续步骤单独撰写**）。

**本次只要「{rlabel}」的 {CANDIDATE_ITEMS} 条候选，不要输出另一个地域的新闻**
（另一个地域由另一次独立选题负责）。
条数比最终需要的 {PER_REGION_ITEMS} 条多出不少，因为系统会做一轮
硬性过滤——剔除消费电子/编造数字/英文残留的条目，需要留有足够冗余。
**候选必须按产业价值从高到低排序**，最终会优先取靠前的条目。

{CANDIDATE_ITEMS} 条候选必须覆盖 4 类，**每类至少 3 条候选、争取 4 条**：

{"- policy 政策发布：**中国政府部门/机构发布的、与人工智能相关的政策文件**——\n  国务院/中办国办/工信部/国家发展改革委/中央网信办/科技部/国家数据局/财政部/\n  商务部等部委，或省级、市级人民政府及主管部门。标题里要写出**发布主体 + 文件名**\n  （如\"工信部印发《“人工智能+软件”专项行动实施方案》\"）。素材里来源标注为\n  \"中国政府网\"的条目就是中国政府网政策文件库里的原文，是 policy 的首选。"
 if region == "cn" else
 "- policy 政策发布：**境外政府或官方监管机构正式发布的、与人工智能相关的法规、\n  行政令、监管规则或国家战略**——如欧盟委员会/欧洲议会、美国白宫与联邦机构\n  （FTC/商务部/NIST）、英国、日本、韩国、新加坡等政府部门的正式文件。\n  标题里要写出**发布主体 + 法规或文件名称**。企业自己的合规声明、行业倡议、\n  高管表态都不算，**不要拿一条普通动态来充当政策发布**。"}
- tech 技术突破：模型/算法/芯片/算力/产品技术本身的进展
- industry 产业动态：企业合作、产品上市、产能布局、行业趋势、企业业绩
- capital 投融资：融资、并购、IPO、估值变化

**「政策发布」的严格口径（务必遵守，这是最容易被误标的一类）**：
{"- 只有**中国官方主体**发布的政策才算 policy。\n- **外国的法案、监管、行政令不算本批的「政策发布」**（本批只要国内的），\n  素材里若出现境外监管内容请归入 **industry 产业动态**。\n- 仅出现\"监管\"\"标准\"\"合规\"\"政策\"这类泛泛字样的新闻也**不算** policy——\n  政策发布必须有明确的官方发布主体和具体文件名/文号。"
 if region == "cn" else
 "- 只有**境外政府部门/官方监管机构**正式发布的法规或行政令才算 policy。\n- 若素材里其实是国内部委发文，本批不要选它（另一次选题会收）。\n- 仅出现\"监管\"\"标准\"\"合规\"这类泛泛字样、或只是企业表态的新闻**不算** policy。"}
- **宁缺勿滥**：若当日素材里确实没有合格的政策发布，policy 可以只给 1 条
  甚至 0 条，其余额度分给另外三类，**绝不能拿一条别的动态来充当政策发布**。

**四类缺一不可**：本批「{rlabel}」最终要凑齐"四类各 2 条"（共 {PER_REGION_ITEMS} 条），
而系统只会从你给的候选里挑，你少给某一类，这一批就一定会缺那一类。
实测最容易漏的是 industry 产业动态（企业合作、新品上市、产能与订单、行业数据
这类新闻），请专门找几条。若某类当日素材确实太少，也至少要给 2 条。

分类口径（必须严格按新闻实质判断，宁缺勿错）：
- 若某一类当日确实没有对应新闻（极少），该类允许为 1 条，其它类补足；
- **不要为了凑齐"每类 3 条"而错标分类**，错标分类比数量不均衡严重得多；
- 企业发生安全事故、被攻击、被罚款等负面事件属于"产业动态"，不要标成"技术突破"；
  只有当新闻本身是技术能力/模型能力的进展时才用"技术突破"。

============== 标题写作标准（本项目定版风格，务必逐条对齐）==============

【标题：20-32 字，三要素齐全】
1. **主体 + 动作 + 结果**齐全。主体必须是具体机构名或公司名（如"国家发改委""交通运输部""Stripe""宇树科技"），禁止"某公司""相关部门""相关负责人"这类模糊主体。
2. **尽量带数字**：金额、数量、规模、时间、比例。参考基准：定版风格中 56% 的标题含数字。
3. **约三分之一的标题使用双分句**（逗号连接）：前半句陈述事实，后半句点出结果或意义。
4. 可用冒号引出细节（如"国家发改委：加快人工智能法立法进程"）。
5. 严禁丢掉主体或事件信息的空泛写法。
6. **标题里不得出现素材摘要中没有的数字**——系统会逐条核验，对不上就作废。
7. **必须译成中文**：素材标题是英文的，先理解再重写为中文标题，禁止直接把英文原句当标题（如"Rapidly scaling online storage"这种一律作废）。

✅ 标题范例（照此风格撰写）：
- 两部门联合发布AI计量体系指引，破解测不准与数据荒
- 北京亦庄建成首个词元工厂，日产1.4万亿词元
- Stripe 75亿美元收购OpenRouter
- 宇树科技科创板挂牌，人形机器人第一股诞生
- 摩根士丹利再上调中国人形机器人出货预期至5万台
- 交通部发布AI+交通场景方案41个
❌ 禁用写法（实测出现过，一律禁止）：
- "AI政策窗口开放"（未说明是谁提出的什么政策）
- "Nvidia解释增长原因"（未说明向谁解释、解释哪项增长）
- "某公司面临挑战"
- "Rapidly scaling online storage"（英文原句未翻译）

【选题纪律】
1. **绝对排除**与 AI 产业无关的内容：消费电子新品（手机/相机/耳机/显示器/笔记本）、汽车新车与试驾（含 MPV/SUV 官图）、灯光与外设软件、操作系统更新（Windows/iOS/安卓的系统或功能更新）、产品与发布会预告、游戏影视娱乐、体育赛事、社会新闻——素材里出现也不要选。
2. 选题限于产业与技术范畴：判断标准是"这条新闻是否直接反映 AI 产业或技术本身的变化"。凡属个人公开表态、社会活动、与产业无关的公共事务，一律不选。
3. 不要选用"早报/日报/盘点/汇总/速览"这类聚合内容。
4. **标题必须忠实于原文事实**：素材多为英文，须准确理解后再译为中文，不得截取英文原句、不得把原文没有的判断归纳进标题。
5. **标题不得泛化**：必须保留原文的核心主体与事件（谁做了什么）。反面示例（实测出现过，一律禁止）："AI政策窗口开放"（没说是谁提的什么政策）、"Meta调整AI建议功能"（没说调整什么、为什么）、"Nvidia解释增长原因"（没说是谁问的、解释了哪项增长）、"AI供电架构问题"（主体和结论都丢失）、"发布脑机接口标准"（丢了主体"我国"）。

硬性要求：
1. 只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记。
2. 对象格式严格为：
{{"summary":"一句话概括今日AI产业要点，不超过80字","items":[{{"cat":"policy","title":"标题20-32字","source":"媒体名","url":"https://原文链接"}},...]}}
   **注意：items 里不要写 desc 字段**，正文留待后续步骤生成。
3. source 填媒体简称（如 IT之家、TechCrunch、The Verge），url 必须从上方素材中挑选真实 URL，禁止编造、拼接或改写。**同一条素材只能出现一次**。
4. title 用中文，控制在 20-32 字，须是新闻事实的准确概括，不要加评价性形容词。
5. **summary 只能概括本次 items 里实际收录的条目**，不得提及未收录的新闻。系统会核对，出现未收录内容视为错误。"""


def clean_desc_output(text):
    """把阶段 B 的裸文本输出清洗成可直接入库的一段正文。

    模型偶尔会带出"正文："前缀、markdown 围栏、分点标记或 JSON 外壳；
    这些若不剥掉，既污染页面，也会让字数统计虚高、掩盖正文过短的事实。
    """
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    # 模型若仍返回 JSON 外壳，取其中的正文字段
    if t.startswith("{"):
        try:
            o = json.loads(t[t.find("{"):t.rfind("}") + 1])
            if isinstance(o, dict):
                t = (o.get("desc") or o.get("text") or o.get("content")
                     or o.get("正文") or t)
        except Exception:
            pass
    # 去掉提示词回声（"正文："、"第一部分：约30字"等）
    t = re.sub(r"^(正文|答复|答案|输出)\s*[:：]\s*", "", t)
    t = re.sub(r"第一部分[（(][^）)]{0,20}[）)]\s*[:：]?", "", t)
    t = re.sub(r"^(第[一二三四]部分)\s*[:：]?\s*", "", t)
    # 分点标记（模型偶发）→ 去掉，正文必须是一段
    t = re.sub(r"(?m)^\s*[-*•]\s*", "", t)
    t = re.sub(r"\s*\n+\s*", "", t)
    return t.strip()


def _cn_amount(v):
    """把绝对数值还原成中文口径的金额表述（120亿元 / 5000万元 / 3000元）。

    [2026-09-14] 修复提示里必须用"人话"点名那个数字。
    最初直接格式化绝对值，模型收到的是"金额约12,000,000,000"——
    既与它自己写的"120亿元"对不上号，也容易被当成两个不同的数字。
    """
    if v >= 1e8:
        return f"{v / 1e8:g}亿元"
    if v >= 1e4:
        return f"{v / 1e4:g}万元"
    return f"{v:,.0f}元"


def _fmt_token(tok):
    """把 risky_tokens 的 token 还原成人话，供重写提示指名点姓地引用。"""
    try:
        kind, _, val = str(tok).partition(":")
        v = float(val)
    except Exception:
        return str(tok)
    if kind == "amt":
        return f"金额约{_cn_amount(v)}"
    if kind == "pct":
        return f"百分比{v:g}%"
    if kind == "x":
        return f"倍数{v:g}倍"
    if kind == "mag":
        return f"数字{_cn_amount(v)}" if v >= 1e4 else f"数字{v:,.0f}"
    return str(tok)


def _source_numbers(m, cap=12):
    """列出这条素材里**真实出现过的数字表达**，供"数字对不上"的修复提示使用。

    [2026-09-14 新增] 真实复验（run 34821688274）里 10 条正文因数字对不上被回炉，
    而修复提示只说"素材里没有这些数字"，模型只能再猜一次，猜错又被回炉，
    三轮耗尽整条丢弃 —— 9 条候选就是这样白白损失的。
    把可用数字直接摊开给模型看，比让它反复试错便宜得多。

    返回形如 "50个、2027年、30%、1830亿美元" 的字符串（去重、按出现顺序、截断）。
    """
    text = ((m.get("title") or "") + " " + (m.get("summary") or "")).strip()
    if not text:
        return ""
    seen, out = set(), []
    # 先抓金额/百分比/倍数这类"容易写错"的表达（带量级或单位的）
    for t in sorted(risky_number_phrases(text)):
        if t not in seen:
            seen.add(t)
            out.append(t)
    # 再抓年份、数量这类普通数字表达（带中文量词或"年/月/日"）
    for mm in re.finditer(r"\d+(?:\.\d+)?(?:万亿|千亿|百亿|亿|万|千|百)?"
                          r"(?:美元|元|倍|%|个|家|款|台|套|种|项|条|座|张|"
                          r"年|月|日|人|次|例|篇|卡|层|级|G|GB|TB|B|K|M)?",
                          text):
        g = mm.group(0)
        if len(g) < 2:          # 单个裸数字不进列表（如"7"这种噪音太多）
            continue
        if g not in seen:
            seen.add(g)
            out.append(g)
    return "、".join(out[:cap])


def build_desc_prompt(m, attempt=0, fix=None):
    """阶段 B：单条素材 → 110-150 字正文（三段结构化填空）。

    [2026-09-14] 用"三段结构化填空 + 自数字数"而不是笼统说"写长一点"：
    实测笼统要求（完整提示词 13983 字符）产出正文均 51 字，
    而把任务窄化成"单条素材、三段配比、不足 110 字必须回填细节"后，
    模型才会真正把摘要里的事实细节铺开。

    fix 是上一轮的体检结果（dict），用于把笼统重试改成定向修复：
      · {"short": True}                    → 字数不够
      · {"numbers": {"amt:...", "pct:40"}} → 用了素材里没有的数字
      · {"english": True}                  → 残留整句英文
      · {"leak": True}                     → 正文里混进了提示词指令
      · {"vague": ("引起广泛关注",)}        → 写了空泛评价/凑字填充

    [2026-09-14 复验后重写] 修改要求由"塞在摘要正下方"改为"提到最前面的
    独立区块 + 显式反抄声明 + 首尾双重提示"。
    原因见 run 34819377914 的真实产出，第 4 条正文长这样：
        "OpenAI CEO Sam Altman表示，公司虽已秘密提交IPO申请，但不会在今年
        上市。上一轮不合格，本次必须逐条修正，OpenAI将不会在2026年公开上市。"
    —— 模型把指令原文当成正文吸收了，而且这条 83 字刚好压过 MIN_DESC_LEN
    的线、数字也对得上，一路漏进最终结果。指令离"素材"越近、越像正文，
    被抄的概率越高；所以现在把它放到最前面并明确标注"不是新闻内容"，
    同时在 desc_issues 里加 leak 后验兜底（双保险）。
    """
    title = (m.get("title") or "").strip()
    sm = (m.get("summary") or "").strip()[:600]
    src = m.get("source") or ""

    # —— 构造【修改要求】区块（仅重写轮次有）——
    rules = []
    if attempt and fix:
        if fix.get("short"):
            rules.append(
                "字数不足：上一版不足 90 字。请把第二部分（关键细节）充分展开，"
                "把摘要里所有可核验的事实（金额、数量、时间、占比、技术规格、"
                "覆盖范围、合作方）逐条写进去；若摘要确实只有一两句、铺不开，"
                "就把事件背景、涉及主体、适用范围、时间安排、后续计划讲清楚，"
                "写到 90-110 字即可，**绝对不要靠编造数字来凑长度**。"
            )
        bad = fix.get("numbers") or set()
        if bad:
            # 同一个金额会同时产出 amt: 与 mag: 两个 token，展示时只留更具体的 amt:
            ams = {t.split(":", 1)[1] for t in bad if t.startswith("amt:")}
            shown = {t for t in bad
                     if not (t.startswith("mag:") and t.split(":", 1)[1] in ams)}
            names = "、".join(sorted(_fmt_token(t) for t in shown))
            # 只告诉模型"错了"是不够的 —— 它得知道"能用什么"。
            # 真实复验（run 34821688274）里 10 条正文因数字对不上被回炉，
            # 模型收到"素材里没有这些数字"后只能瞎猜，往往又猜错一次。
            usable = _source_numbers(m)
            usable_note = (
                f"本条素材里**可以放心使用**的数字只有：{usable}。"
                if usable else
                "本条素材里**没有任何可用数字**，请干脆不要写数字。"
            )
            rules.append(
                f"数字对不上：上一版写入了{names}，但新闻素材里并没有这些数字。"
                + usable_note +
                "请改用上面这些可用数字，或删掉该数字、改写成不含数字的事实表述。"
                "注意：一个数字都不许自己推算或从其他新闻里借。"
            )
        if fix.get("english"):
            rules.append(
                "英文残留：上一版夹了整句英文。请把整句英文译成中文，"
                "只保留公司名/产品名/模型名/技术术语的英文原名。"
            )
        if fix.get("leak"):
            rules.append(
                "混入了指令：上一版把写作要求本身（类似“上一轮不合格”这样的字样）"
                "当成新闻写进了正文。正文里只允许有这条新闻的内容，"
                "一个字的说明性文字都不许出现。"
            )
        vague = tuple(fix.get("vague") or ())
        if vague:
            rules.append(
                "空泛表述：上一版出现了" + "、".join(f"“{v}”" for v in vague)
                + "这类没有信息量的评价或凑字填充。请删掉它们，"
                "换成可核验的事实（数字、时间、机构名、技术规格）。"
            )
    if rules:
        fix_block = (
            "\n【修改要求】以下是针对你**上一版**输出的修改指令，属于工作说明，"
            "**不是新闻内容**。正文只能是这条新闻本身，"
            "这些指令的字眼一个都不许出现在正文里（系统会检查，出现则整条作废）。\n"
            + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
            + "\n【修改要求结束】\n"
        )
    else:
        fix_block = ""

    return f"""请把下面这条新闻写成一段中文产业动态正文，用于行业简报。{fix_block}
【新闻素材】
标题：{title}
来源：{src}
原文摘要：{sm if sm else "（无摘要，只能依据标题撰写，请谨慎保持保守表述）"}

【正文写法】请严格按以下三部分依次写出，然后用逗号/句号自然连成**一段话**，总长 **110-150 字**：
- 第一部分（约 30 字）：谁（具体机构/公司全名）做了什么（发布/融资/推出了什么）
- 第二部分（约 70 字）：关键细节，尽可能写出摘要里的具体数字（金额、数量、时间、占比、技术规格、覆盖范围、合作方）
- 第三部分（约 35 字）：影响、对比或后续计划，用事实表达（如“较此前2.8万台的预测近乎翻倍”）

【写作纪律】
- 写完请自己数一遍字数：**不得少于 90 字**（低于 90 字一律不合格）；理想长度 110-150 字，请尽量向 119 字（定版实测平均）靠拢。
- **但也不要写超 160 字**。若已超过 160 字，请优先删掉第三部分里的铺垫与评价性语句，保留事实。
- **只允许使用摘要中出现过的数字，不得自己编造**（系统会校验，编造数字整条作废）。
- **摘要信息量确实不足时，不要靠编数字凑字数**：把事件背景、涉及主体、适用范围、时间安排、后续计划讲清楚即可，写到 90-110 字之间系统同样接受。**宁可写得短一点，也不许出现摘要里没有的数字。**
- 禁止“意义重大”“里程碑式”“引发广泛关注”“覆盖范围广泛”“合作方众多”这类没有信息量的空泛评价与凑字填充，第三部分也必须用事实说话。
- 以中文书面语为主，公司名/产品名/模型名/技术术语保留英文原名，不要残留整句英文。
- 不要分点罗列、不要小标题、不要输出三部分的标题，直接输出这一段正文本身。
- 不要加引号包裹，不要写“正文：”之类的前缀。
- 不要复述或引用本提示里的任何要求、说明文字，正文只能是这条新闻的内容本身。

正文："""


# 提示词指令残留在正文里的特征短语。
# [2026-09-14] 真实 API 复验（run 34819377914）里模型把"上一轮不合格，
# 本次必须逐条修正"当正文抄了，且该条刚好压线过 MIN_DESC_LEN，一路漏进成品。
# 提示词侧已改成"指令前置 + 明确声明不是新闻内容"，这里再做一道后验兜底：
# 正常新闻正文不可能出现这些说明性字眼，命中即可判定为污染。
_PROMPT_LEAK_MARKS = (
    "上一轮", "上一版", "不合格", "逐条修正", "必须修正", "字数不足",
    "数字对不上", "英文残留", "本次必须", "写作纪律", "修改要求",
    "原文摘要", "系统会校验", "整条作废", "请二选一", "混入指令",
    "空泛表述", "正文写法", "新闻素材", "以下三部分", "请严格按",
)

# 空泛评价与凑字填充。8 月定版正文里不出现这类说法。
# [2026-09-14] 复验产出里第 6 条结尾"此法案出台，引起广泛关注"、
# 第 7 条"覆盖范围广泛，合作方众多"，都是把提示词的禁用词换个说法、
# 或为凑字数堆的废话。注意"值得关注"这类过于常用的表达不收，
# 否则会误杀正常行文（如"值得关注的是，该方案将于明年落地"）。
_VAGUE_MARKS = (
    "引发广泛关注", "引起广泛关注", "备受关注", "引发关注", "引发热议",
    "意义重大", "重要意义", "里程碑", "覆盖范围广泛", "合作方众多",
    "前景广阔", "值得期待", "开启新篇章", "注入新动能", "迈上新台阶",
    "广泛好评", "广受关注",
)


def prompt_leak(desc):
    """正文里是否混进了提示词的指令文字（返回命中的短语，空元组=干净）。"""
    return tuple(w for w in _PROMPT_LEAK_MARKS if w in (desc or ""))


def vague_phrases(desc):
    """正文里的空泛评价/凑字填充（返回命中的短语，空元组=干净）。"""
    return tuple(w for w in _VAGUE_MARKS if w in (desc or ""))


def desc_issues(desc, material_text):
    """体检一段正文，返回问题字典（空 dict = 合格）。

    把原来"任一不合格就丢弃整条"的三道硬门槛，收敛成一份可修复清单，
    交给 build_desc_prompt 做定向重写。只有重写若干轮仍不合格才真正丢弃。

    [2026-09-14] 由 3 项检查扩到 5 项：新增 leak（提示词指令污染）与
    vague（空泛评价/凑字填充）。这两项都是真实 API 复验暴露出来的漏网类型。
    """
    issues = {}
    if len(desc) < MIN_DESC_LEN:
        issues["short"] = True
    bad = ungrounded_numbers(desc, material_text)
    if bad:
        issues["numbers"] = bad
    if stray_english_count(desc) >= 3:
        issues["english"] = True
    leak = prompt_leak(desc)
    if leak:
        issues["leak"] = leak
    vague = vague_phrases(desc)
    if vague:
        issues["vague"] = vague
    return issues


def issues_brief(issues):
    """把体检结果压成一行可读的原因串，供日志使用。

    [2026-09-14] 抽成公共函数：news_pipeline 与 backfill_news 都要打这行日志，
    两边各写一份的话，以后每加一项检查必然漏改其中一处。
    """
    why = []
    if issues.get("short"):
        why.append("字数不足")
    if issues.get("numbers"):
        why.append("数字对不上:" + ",".join(sorted(issues["numbers"])))
    if issues.get("english"):
        why.append("英文残留")
    if issues.get("leak"):
        why.append("混入指令:" + "、".join(issues["leak"][:2]))
    if issues.get("vague"):
        why.append("空泛表述:" + "、".join(issues["vague"][:2]))
    return "｜".join(why)


def parse_llm_json(content):
    """从 LLM 输出中稳健提取 JSON 对象（含 summary 与 items）。

    [2026-09-14] 增加截断容错。原实现直接 json.loads(text[start:end+1])，
    一旦模型输出被 max_tokens 截断（最后一个 item 只写了一半），
    整体解析失败 → 这一次生成完全白费，重试又要重新消耗一次调用。
    现在解析失败后退化为"逐条抢救已完整输出的 item 对象"，
    能救出多少算多少（配合上层重试，把"整批丢失"降级为"少一两条"）。
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start == -1:
        raise ValueError(f"LLM 输出未找到 JSON 对象: {text[:200]}")
    end = text.rfind("}")
    if end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    # —— 截断容错：按花括号配对扫描，抢救出每个完整的 item 对象 ——
    salvaged, items = {}, []
    seen_titles = set()
    for m in re.finditer(r"\{", text):
        depth, close = 0, -1
        for j in range(m.start(), len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close == -1:          # 这一层没闭合（截断点），跳过
            continue
        frag = text[m.start():close + 1]
        if '"title"' not in frag or '"cat"' not in frag:
            continue
        try:
            obj = json.loads(frag)
        except Exception:
            continue
        t = str(obj.get("title", "")).strip()
        if t and t not in seen_titles:
            seen_titles.add(t)
            items.append(obj)
    if not items:
        raise ValueError(f"LLM 输出 JSON 解析失败且无法抢救: {text[:200]}")
    sm = re.search(r'"summary"\s*:\s*"(.*?)"\s*,', text, re.S)
    if sm:
        salvaged["summary"] = sm.group(1)
    salvaged["items"] = items
    log(f"  ⚠ LLM 输出疑似被截断，已抢救出 {len(items)} 条完整条目")
    return salvaged


def clean_for_js(value):
    """JS 字符串安全：内部英文双引号一律转中文引号，去首尾空白"""
    if not isinstance(value, str):
        value = str(value)
    return value.replace('"', "“").strip()


# 分类纠偏关键词
# [2026-09-18 已废弃] 原 POLICY_HINTS / POLICY_STRONG 用的是"政策/监管/法案/
#   提案"这类泛词，任何带"监管"字样的稿子都能算政策发布（外国议员提案、企业
#   合规新闻都混了进来）。政策发布的判据已改为 is_cn_official_policy()
#   （国内官方主体 + 政策动作词 + AI 主题），不再依赖本表，故整表删除。
CAPITAL_HINTS = ["融资", "并购", "收购", "ipo", "上市", "估值", "投资", "注资",
                 "增资", "参股", "领投", "跟投", "募资", "轮"]
NEGATIVE_HINTS = ["黑客", "攻击", "入侵", "罚款", "起诉", "诉讼", "争议", "泄露",
                  "宕机", "故障", "被罚", "违规", "反垄断", "泄密",
                  # [2026-09-14] 补：负面事件通用表述，避免"某公司面临网络安全
                  # 问题"被标成技术突破
                  "安全问题", "网络安全问题", "陷入困境"]

# 明确的资本类信号：用于把【被错标成其它类】的投融资新闻改判回来。
# 刻意不含裸"上市"/"投资"——它们会和"产品上市""战略投资"混淆，另由
# _capital_signal() 结合产品发布词做区分。
CAPITAL_STRONG = [
    "融资", "并购", "收购", "ipo", "估值", "募资", "领投", "跟投",
    "注资", "增资", "参股", "首次公开募股", "天使轮", "种子轮",
    "轮融资", "融资轮", "上市计划", "推迟上市", "暂缓上市", "股票",
]
# 明确的政策类信号：[2026-09-18 已废弃]
# 原表把"法案/立法/监管机构/提案"当成政策强信号，正是外国议员提案挤占政策发布
# 名额的来源。政策判据已由 is_cn_official_policy() 接管，故删除。
# 产品发布语境：命中则裸"上市"不算资本信号（"新GPU产品上市"≠IPO）
_PRODUCT_LAUNCH = [
    "产品上市", "新品上市", "新机上市", "开售", "首销", "预售", "发售",
    "正式上市销售", "开卖",
]

# ---------------------------------------------------------------------------
# 「政策发布」的国内官方口径（2026-09-18 用户要求）
#
# 用户原话："现在的产业动态，尤其是政策发布，尽量用国内官方政府发的人工智能
#   相关政策，不要随便一条动态都叫政策发布。"
# 此前 POLICY_HINTS/POLICY_STRONG 里塞的是"政策/监管/标准/法案/提案"这类**泛词**，
# 结果任何带"监管"字样的稿子（含外国议员提案、企业合规新闻）都能算政策发布，
# 真正的部委文件反而不突出。现在改为三条件同时满足才准入：
#   ① 国内官方主体  ② 政策动作词  ③ AI 相关主题
# 外国的法案/监管一律不算政策发布（归产业动态）；泛泛的"监管/标准/政策"字样
# 不再构成政策依据。
# ---------------------------------------------------------------------------

# ① 国内官方主体。刻意**不含**"政府""官方""监管部门""监管机构"这类泛称——
#    它们既能指中国政府也能指外国政府，正是此前误判政策的主因。
CN_OFFICIAL_ORGS = [
    "国务院", "中共中央", "中央办公厅", "国务院办公厅", "中办", "国办",
    "工信部", "工业和信息化部", "国家发展改革委", "国家发改委", "发改委",
    "科技部", "科学技术部", "中央网信办", "国家网信办", "网信办",
    "国家数据局", "财政部", "商务部", "教育部", "人力资源社会保障部", "人社部",
    "国家能源局", "国家卫健委", "国家药监局", "市场监管总局", "国家市场监管总局",
    "国家标准化管理委员会", "国家标准委", "中国人民银行", "金融监管总局",
    "证监会", "国家统计局", "国资委", "中国科学院", "中国工程院",
    "国家知识产权局", "交通运输部", "交通部", "住房和城乡建设部", "农业农村部",
    "国家医保局", "国家铁路局", "民航局", "国家档案局", "国家密码管理局",
]
# 地方政府（省级/市级）发文同样算官方政策
_CN_LOCAL_ORG = re.compile(
    r"[\u4e00-\u9fff]{2,6}(?:省|市|自治区|直辖市)"
    r"(?:人民政府|政府办公厅|工信厅|经信厅|工业和信息化厅|发展改革委|发改委|"
    r"数据局|网信办|科学技术厅|科技厅|大数据局)"
)

# 境外官方主体（"国外"板块的政策发布准入条件，2026-09-21 新增）。
# 只收**政府与法定监管机构**：企业自己的合规声明、行业协会倡议、
# 议员的个人提案都不算，否则"某公司承诺遵守AI安全准则"又会被塞进政策发布。
INTL_OFFICIAL_ORGS = [
    "欧盟委员会", "欧洲议会", "欧盟理事会", "欧盟", "欧洲联盟",
    "白宫", "美国国会", "参议院", "众议院", "美国商务部", "美国司法部",
    "美国联邦贸易委员会", "美国联邦通信委员会", "美国财政部", "美国能源部",
    "美国国家标准与技术研究院", "美国证监会", "美国证券交易委员会",
    "英国政府", "英国科学创新与技术部", "英国信息专员办公室",
    "日本政府", "日本总务省", "日本经济产业省", "韩国政府", "韩国科学技术信息通信部",
    "印度政府", "印度电子和信息技术部", "新加坡政府", "新加坡资讯通信媒体发展局",
    "阿联酋政府", "沙特政府", "法国政府", "德国政府", "加拿大政府",
    "澳大利亚政府", "以色列创新局", "荷兰政府", "意大利政府", "巴西政府",
    "联合国", "经济合作与发展组织", "二十国集团",
    "eu commission", "european commission", "european parliament",
    "white house", "congress", "senate", "ftc", "nist", "fcc", "doj",
    "uk government", "european union",
]

# ② 政策动作词：官方"发文"的典型动词/文种
POLICY_ACTIONS = [
    "印发", "发布", "出台", "下发", "颁布", "实施", "部署", "启动",
    "通知", "意见", "办法", "规定", "规划", "方案", "行动计划", "实施方案",
    "条例", "批复", "征求意见", "试点", "决定", "公告", "清单", "指引",
    "纲要", "细则", "措施", "行动计划", "工作要点", "暂行规定", "管理办法",
]

# ---------------------------------------------------------------------------
# 地域归属判定（2026-09-21 新增）
#
# 用户要求把每日动态拆成"国内 8 条 + 国外 8 条"两个板块，所以每条新闻都要带
# 一个地域标签。地域看的是**新闻主体**（谁做的这件事），不是媒体来源：
# 量子位报道 OpenAI 属于国外，TechCrunch 报道比亚迪属于国内。
#
# 判定顺序（强 → 弱）见 region_signal()：
#   ① 中国政府网政策文件库原文 → 国内（决定性强）
#   ② 标题里出现部委/地方政府 → 国内（决定性强）
#   ③ 标题权重 3 倍、正文 1 倍地统计国内外主体词，得分高者胜
#   ④ 双方都没命中 → 交给模型在阶段 A 填的 region，最后才用"标题含拉丁字母"兜底
#
# 注意：中国台湾的科技企业（台积电/鸿海/联发科等）计入国内主体。
# ---------------------------------------------------------------------------

# 国内主体：企业简称、平台名、运营商、科研机构。
# 含"中国"二字兜底（"中国AI企业""我国"这类共现），但它在与国外主体打平时
# 会被下面的"平手优先级"压过，所以"美国限制中国AI芯片"仍会判为国外。
CN_ORGS = [
    "中国", "我国", "国产", "国内",
    "华为", "荣耀", "小米", "联想", "中兴", "紫光", "浪潮", "曙光", "新华三",
    "阿里", "阿里巴巴", "蚂蚁", "百度", "腾讯", "字节", "字节跳动", "抖音",
    "京东", "美团", "拼多多", "网易", "快手", "携程", "滴滴", "小红书", "微信",
    "商汤", "旷视", "云从", "依图", "科大讯飞", "智谱", "月之暗面", "深度求索",
    "百川智能", "阶跃星辰", "零一万物", "minimax", "面壁智能", "智元",
    "宇树", "优必选", "傅利叶", "银河通用", "星动纪元", "松延动力",
    "寒武纪", "燧原", "壁仞", "摩尔线程", "地平线", "黑芝麻", "天数智芯",
    "中芯国际", "长江存储", "华虹", "海光", "飞腾", "龙芯", "中微公司",
    "比亚迪", "蔚来", "小鹏", "理想汽车", "宁德时代", "大疆", "三一重工",
    "中国移动", "中国联通", "中国电信", "中国广电", "国家电网", "南方电网",
    "三大运营商", "中国电子", "中国电科", "中科院", "中国信通院", "赛迪",
    "之江实验室", "鹏城实验室", "北京智源", "上海人工智能实验室", "国家超算",
    "东数西算", "雄安", "亦庄", "临港", "中关村",
    # 部委简称（只用于地域判定，不用于政策准入——政策准入仍走 CN_OFFICIAL_ORGS
    # 的全称口径）。放进这张表是因为标题里只有"两部门"这类泛称时，
    # 摘要里的部委名是唯一的国内信号。
    "市场监管总局", "国家发改委", "发改委", "工信部", "国家数据局",
    "交通运输部", "科技部", "财政部", "商务部", "网信办", "工信部",
    # 中国台湾企业（台湾是中国的一部分，归国内）
    "台积电", "鸿海", "富士康", "联发科", "纬创", "广达", "和硕", "日月光",
]

# 国外主体：企业、政府与国际组织。
# 判据是"这条新闻的主体是不是境外机构"，因此既收英文名也收常见中文译名。
INTL_ORGS = [
    "openai", "anthropic", "谷歌", "google", "deepmind", "微软", "microsoft",
    "meta", "苹果", "apple", "亚马逊", "amazon", "aws", "英伟达", "nvidia",
    "amd", "英特尔", "intel", "oracle", "甲骨文", "salesforce", "ibm",
    "特斯拉", "tesla", "xai", "mistral", "cohere", "perplexity", "midjourney",
    "stability", "hugging face", "scale ai", "databricks", "snowflake",
    "palantir", "sap", "三星", "samsung", "索尼", "sony", "软银", "softbank",
    "uber", "waymo", "zoox", "figure ai", "boston dynamics", "qualcomm",
    "高通", "arm", "asml", "博通", "broadcom", "美光", "micron", "github",
    "reddit", "linkedin", "stripe", "coinbase", "adobe", "servicenow",
    "workday", "siemens", "西门子", "bosch", "博世", "丰田", "toyota",
    "本田", "honda", "宝马", "bmw", "大众", "volkswagen", "奔驰", "mercedes",
    "福特", "ford", "rivian", "lucid", "spacex", "x平台",
    # 境外政府/国际组织（"美国""欧盟"这类既可能是发布主体也可能是背景，
    # 所以只算 1 票，靠正文与标题加权区分）
    "美国", "白宫", "欧盟", "欧洲", "英国", "日本", "韩国", "德国", "法国",
    "印度", "新加坡", "阿联酋", "沙特", "俄罗斯", "加拿大", "澳大利亚",
    "以色列", "荷兰", "瑞士", "巴西", "意大利", "西班牙", "越南", "泰国",
    "联合国", "nasa", "nist", "ftc", "doj", "sec", "gdpr",
]
# 上面这些国家名在纯计数时会互相干扰（"美国"既是主体也可能是背景），
# 因此额外记录一组"国家/地区名"，在打平时用于判定"到底谁在做这件事"。
_REGION_PLACE = ["美国", "欧盟", "欧洲", "英国", "日本", "韩国", "德国", "法国",
                 "印度", "新加坡", "阿联酋", "沙特", "俄罗斯", "加拿大",
                 "澳大利亚", "以色列", "荷兰", "瑞士", "巴西", "中国", "我国"]
# 兜底判定时忽略的中性缩写：这些词在任何一条中文 AI 标题里都可能出现，
# 不代表主体在境外。
_LATIN_HINT_SKIP = {
    "ai", "ar", "vr", "mr", "xr", "gpu", "cpu", "npu", "tpu", "api", "ip",
    "erp", "crm", "saas", "paas", "iaas", "agi", "llm", "it", "ceo", "ipo",
    "ev", "pc", "os", "ui", "ux", "sql", "rag", "moe", "vla", "sdk",
}


def _count_orgs(text, table):
    """统计 text 命中的主体词个数（返回 (命中数, 命中词列表)）。

    纯拉丁词（meta / arm / sec 这类）必须按**词边界**匹配，否则
    "metadata""security""harm" 会被当成公司名，把国内新闻误判成国外。
    含中文或空格的词走子串匹配（中文没有词边界）。
    同一个词只计一次；命中的词列表用于日志，便于人工复核判定依据。
    """
    t = str(text or "")
    tl = t.lower()
    hit = []
    for k in table:
        if k in hit:
            continue
        kl = k.lower()
        if not kl:
            continue
        if re.fullmatch(r"[a-z0-9]+", kl):
            if re.search(r"(?<![a-z0-9])" + re.escape(kl) + r"(?![a-z0-9])", tl):
                hit.append(k)
        elif kl in tl:
            hit.append(k)
    return len(hit), hit


def _leading_place(title):
    """返回标题开头（前 4 字）出现的国家/地区名，没有则返回空串。

    中文新闻标题习惯把"施动者"放在最前面："美国商务部将 12 家中国 AI 芯片企业
    列入实体清单"里，美国是主体、中国是对象，两地名都命中时只靠计数分不出来，
    看谁在句首最稳。
    """
    head = str(title or "")[:4]
    for p in _REGION_PLACE:
        if p in head:
            return p
    return ""


def region_signal(title, desc="", source="", url=""):
    """地域的**确定性**信号：返回 (region, 强度)，无把握时返回 (None, 0)。

    强度 2 = 决定性（官方发布主体），可直接覆盖模型判断；
    强度 1 = 主体计数占优，也以确定性结果为准；
    强度 0 = 没有任何主体线索，交给模型或拉丁字母兜底。
    """
    title = str(title or "")
    desc = str(desc or "")
    src = str(source or "")
    u = str(url or "")

    if src == "中国政府网" or "gov.cn" in u:
        return "cn", 2

    # 官方主体：国内（部委/地方政府）与境外（外国政府/监管机构）都要看。
    # 只看国内会踩坑——"美国商务部"里含"商务部"，会被当成中国部委发文。
    cn_org = (any(k in title for k in CN_OFFICIAL_ORGS)
              or bool(_CN_LOCAL_ORG.search(title)))
    intl_org = any(k in title for k in INTL_OFFICIAL_ORGS)
    if cn_org and intl_org:
        # 两边都命中（"美国商务部…中国AI芯片企业"）：主体几乎总在句首，
        # 看句首是哪国直接定死，不再往下走主体计数（计数在这种标题里必错——
        # "中国"作为被制裁对象也出现在标题里，会把票数拉平甚至反超）
        lead = _leading_place(title)
        if lead in ("中国", "我国"):
            return "cn", 2
        if lead:
            return "intl", 2
    elif cn_org:
        return "cn", 2
    elif intl_org:
        return "intl", 2

    t_cn = _count_orgs(title, CN_ORGS)[0]
    t_fg = _count_orgs(title, INTL_ORGS)[0]
    d_cn = _count_orgs(desc, CN_ORGS)[0]
    d_fg = _count_orgs(desc, INTL_ORGS)[0]
    # 标题是"这件事发生在谁身上"的最强指示，权重给 3 倍
    s_cn = 3 * t_cn + d_cn
    s_fg = 3 * t_fg + d_fg
    if s_cn > s_fg:
        return "cn", 1
    if s_fg > s_cn:
        return "intl", 1
    # 完全打平（含"国内外地名各一个"和"两边都没命中"）：看句首是谁
    lead = _leading_place(title)
    if lead in ("中国", "我国"):
        return "cn", 1
    if lead:
        return "intl", 1
    return None, 0


def _norm_region_token(r):
    """把模型写的各种地域说法归一成 cn / intl，无法识别时返回空串。"""
    t = str(r or "").strip().lower()
    if t in ("cn", "china", "chinese", "domestic", "国内", "中国", "境内", "国内动态"):
        return "cn"
    if t in ("intl", "int'l", "international", "overseas", "global", "国外",
             "海外", "境外", "国际", "国外动态", "全球"):
        return "intl"
    return ""


def region_of(title, desc="", source="", url="", model_region=""):
    """判定一条新闻属于国内（cn）还是国外（intl）。

    优先级：确定性主体信号 > 模型标注 > 标题是否含拉丁字母。
    最后这层兜底的理由：标题全中文且没有任何可识别主体时（如"一年连融三轮，
    金融AI公司拿下超3亿B轮"），基本是国内媒体在讲国内的事；
    而标题里带 OpenAI/GPT 这类拉丁专名时，主体多半在境外。

    兜底判定里必须先剔掉 AI/GPU/IPO 这类**中性缩写**——它们几乎出现在每一条
    中文标题里，不剔掉的话最后一层永远返回"国外"，等于兜底失效。
    """
    r, _ = region_signal(title, desc, source, url)
    if r:
        return r
    mr = _norm_region_token(model_region)
    if mr:
        return mr
    return "intl" if _has_latin_hint(title) else "cn"


def _has_latin_hint(title):
    """标题里是否有**指向境外主体**的拉丁词（排除 AI/GPU/IPO 这类中性缩写）。"""
    for w in re.findall(r"[A-Za-z]{2,}", str(title or "")):
        if w.lower() not in _LATIN_HINT_SKIP:
            return True
    return False


def normalize_region(region, title, desc="", source="", url=""):
    """融合模型标注与确定性判定，返回最终地域。

    只在确定性判定**有把握**（强度 ≥1）时覆盖模型，否则尊重模型判断——
    模型读得到正文摘要里的上下文，比"标题搜不到主体词"这种弱信号更可靠。
    """
    r, strength = region_signal(title, desc, source, url)
    if strength >= 1:
        return r
    return _norm_region_token(region) or region_of(title, desc, source, url)


def split_material_by_region(material):
    """把素材池按地域劈成两份，供阶段 A 分两次选题使用。

    官方政策素材（中国政府网）必然落在国内池里，这正是用户要的
    "政策发布用国内官方政府发的 AI 相关政策"。
    """
    pools = {r: [] for r in REGIONS}
    for m in material:
        r = region_of(m.get("title", ""), m.get("summary", ""),
                      m.get("source", ""), m.get("url", ""))
        pools[r].append(m)
    return pools

# ③ AI 相关主题词。英文 ai 需词边界（见 has_ai_topic），否则 "said" 会被误命中。
AI_POLICY_TOPICS = [
    "人工智能", "大模型", "模型", "算力", "智能体", "机器人", "数据",
    "算法", "芯片", "智能制造", "数字经济", "信息化", "智能化", "新一代信息技术",
]


def has_ai_topic(text):
    """文本是否与 AI / 智能技术相关（英文 ai 用词边界判定）。"""
    t = (text or "").lower()
    if re.search(r"(?<![a-z])ai(?![a-z])", t):
        return True
    return any(k in t for k in AI_POLICY_TOPICS)


def is_cn_official_policy(title, desc=""):
    """是否为中国官方发布的、与 AI 相关的政策文件（政策发布的准入条件）。

    [2026-09-18] 三条件同时满足才算：国内官方主体 + 政策动作词 + AI 主题。
    返回 False 的条目不会被标成"政策发布"（改判产业动态/投融资），
    从而杜绝"随便一条动态都叫政策发布"。
    """
    text = f"{title or ''} {desc or ''}"
    t = text.lower()
    if not any(k in text for k in CN_OFFICIAL_ORGS) and not _CN_LOCAL_ORG.search(text):
        return False
    if not any(k in text for k in POLICY_ACTIONS):
        return False
    return has_ai_topic(t)


def _capital_signal(text):
    """资本类信号强度（含裸"上市"的语境消歧）"""
    n = sum(1 for k in CAPITAL_STRONG if k in text)
    if "上市" in text and not any(p in text for p in _PRODUCT_LAUNCH):
        n += 1
    return n


def _policy_signal(text):
    """[2026-09-18 已废弃] 政策判据见 is_cn_official_policy()。保留空实现只为
    兼容可能的旧调用点，返回 0 表示"不做泛指词匹配"。"""
    return 0


def is_intl_official_policy(title, desc=""):
    """是否为中国境外政府/官方监管机构正式发布的、与 AI 相关的法规或行政令。

    [2026-09-21] 「国外」板块的四个维度里同样有"政策发布"，但它不能再用
    is_cn_official_policy() 判（那会把所有境外政策都踢成产业动态）。这里把
    「境外官方主体 + 政策动作词 + AI 主题」三条件同样收紧：
    企业合规声明、行业倡议、高管表态、议员个人提案一律不算，
    必须是**政府或法定监管机构正式发布**的规则文本。
    """
    text = f"{title or ''} {desc or ''}"
    if not any(k in text for k in INTL_OFFICIAL_ORGS):
        return False
    # 只看"正式发文"动作；英文侧用词边界匹配 enact/rule/ban 这类联邦规则用语
    if not any(k in text for k in POLICY_ACTIONS) and not re.search(
            r"(?<![a-z])(?:rule|rules|regulation|regulations|act|ban|order|"
            r"mandate|directive|guideline|framework|bill)(?![a-z])", text.lower()):
        return False
    return has_ai_topic(text)


def normalize_category(cat, title, desc, region="cn"):
    """分类确定性纠偏（双向）。

    [2026-09-14 修复] 原实现只会【降级】：模型标的类目缺支撑词就统统丢进
    industry，从不改判到正确的类目。实测后果是投融资常年 0 条、政策发布也
    被吃掉 —— 例如
      "Mecka AI在Sequoia领投的融资中估值近5亿美元"（模型标 industry）
      "OpenAI CEO称2026年上市不合适"（模型标 policy→被降级 industry）
      "美国新提案遏制前沿AI发展，最高监禁20年"（模型标 policy→被降级 industry）
    本该是投融资/政策发布的新闻全被压进产业动态，导致四类失衡、凑不满 8 条。
    现在改为：先用强信号把条目【改判】到资本/政策，再对无支撑的类目降级。

    [2026-09-18 政策口径收紧] 用户要求"政策发布尽量用国内官方政府发布的人工智能
    相关政策，不要随便一条动态都叫政策发布"。政策发布的准入改由
    is_cn_official_policy() 判定（国内官方主体 + 政策动作词 + AI 主题，三者齐备）；
    原先"命中泛指词（监管/标准/法案/提案）即算政策"的判据已废除——
    正是它让外国议员提案、企业合规新闻都挤进了政策发布。

    [2026-09-21 地域化] 页面拆成国内/国外两个板块后，"政策发布"这一维度在两个
    板块里各自成立：**国内板块**只认中国官方发文（is_cn_official_policy），
    **国外板块**只认境外政府/监管机构的正式规则（is_intl_official_policy）。
    判据按 region 选用，避免国外板块的政策条目被国内判据一律踢走。
    """
    text = (str(title or "") + str(desc or "")).lower()
    n_cap = _capital_signal(text)
    if region == "intl":
        pol = is_intl_official_policy(title, desc)
    elif region == "cn":
        pol = is_cn_official_policy(title, desc)
    else:
        pol = is_cn_official_policy(title, desc) or is_intl_official_policy(title, desc)

    # 1) 政策发布：只认该地域的官方 AI 政策（含从产业/技术类改判回来的）
    if cat == "policy" and not pol:
        return "capital" if n_cap else "industry"
    if cat in ("industry", "tech") and pol and not n_cap:
        return "policy"
    # 2) 模型标 capital 但文本没有资本支撑 → 能确认是官方政策就改判，否则降级
    if cat == "capital" and n_cap == 0:
        return "policy" if pol else "industry"
    # 3) 模型标 industry / tech，但文本是明确的资本事件 → 改判回投融资
    if cat in ("industry", "tech") and n_cap and not pol:
        return "capital"
    # 4) 技术突破不得用于负面事件
    if cat == "tech" and sum(1 for k in NEGATIVE_HINTS if k in text):
        return "industry"
    return cat


# 摘要校验时豁免的通用词
_SUMMARY_OK = {"ai", "openai", "chatgpt", "api", "gpt", "ceo", "ipo", "llm", "it"}


def summary_consistent(summary, items):
    """摘要是否只提及本次实际收录的条目。

    摘要是模型自由生成的，容易把没收录的新闻（如"苹果iOS27升级Siri"）
    也写进去。这里检查摘要中出现的英文/版本号标识能否在条目里找到，
    找不到就判定摘要跑偏，改为用条目标题拼装的兜底摘要。
    """
    if not summary:
        return True
    body = " ".join(
        str(it.get("title", "")) + " " + str(it.get("desc", "")) for it in items
    ).lower()
    for m in re.finditer(r"[a-z]{2,}\d*", summary.lower()):
        tok = m.group(0)
        if tok in _SUMMARY_OK:
            continue
        if tok not in body:
            return False
    return True


def fallback_summary(items):
    """由已收录条目拼装的兜底摘要，保证与条目一致"""
    return "、".join(str(it.get("title", "")) for it in items[:3])[:110]


def interleave_by_cell(cands, prefer=1):
    """把候选按"每个（地域 × 维度）格子轮转"重排，让阶段 B 早写的条目天然覆盖 8 格。

    [2026-09-14 新增] 阶段 B 是"写满 DESC_TARGET 条就停手"，
    若照候选原顺序（按产业价值降序）写，前若干条很可能集中在"产业动态"，
    等轮到"投融资"时已经收工了，四类必然缺项——真实 API 实测
    （run 34818610548）就出现过"只有政策发布 2 条 + 产业动态 1 条"。

    [2026-09-21 升级为八格] 目标从"四类各 2"变成"国内四类各 2 + 国外四类各 2"，
    所以轮转键从 cat 换成 (region, cat)。prefer 取 1 表示：第一轮先给每个格子
    各凑 1 条，把 8 格全部铺满，再回头补第二圈。这样"提前收工"时不会出现
    "国内满了、国外空着"或"国外满了、国内空着"。
    格内仍保持价值降序，所以整体价值序不会被破坏太多。
    """
    by_cell = {}
    for i, c in enumerate(cands):
        by_cell.setdefault((c.get("region", "cn"), c["cat"]), []).append(i)
    order, used = [], set()
    for want in range(1, prefer + 1):
        for region in REGIONS:
            for cat in CAT_LABELS:
                pool = by_cell.get((region, cat), [])
                if len(pool) >= want:
                    order.append(pool[want - 1])
                    used.add(pool[want - 1])
    for i in range(len(cands)):            # 其余按原价值序补在后面
        if i not in used:
            order.append(i)
    return [cands[i] for i in order]


_EN_ENTITY = re.compile(r"[A-Za-z][A-Za-z\.\-]{3,}")
_CN_ENTITY = re.compile(
    r"[\u4e00-\u9fff]{2,4}(?:科技|集团|公司|研究院|实验室|大学|银行|证券|"
    r"大学|智能|网络|电子|软件|半导体|机器人|生物|医药|能源|电力|航空|航天|"
    # 政府部门也算主体：「商务部等8部门印发…」与「商务部印发…」是同一件事的两种说法，
    # 只靠企业类后缀识别不到主体，去重就会漏掉这类重复。
    # 后缀取单字（部/委/局/署）看似宽，但要求前缀至少 2 个字，
    # "全部""内部""一部分"这类两字词和"大部"这种单字前缀都不会命中。
    r"总局|部|委|局|署)"
)


def _title_shape(t):
    """把标题拆成 (主体词集合, 2-gram 集合)，用于判两条是否在讲同一件事。"""
    s = re.sub(r"[\s，。：、！？·\-—–“”‘’（）()\[\]【】/]", "", t or "")
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    ents = {w.lower().strip(".") for w in _EN_ENTITY.findall(t or "")}
    ents |= set(_CN_ENTITY.findall(t or ""))
    return ents, grams


def dedupe_similar(items, min_jaccard=0.5):
    """去掉"同一主体 + 同一类别 + 措辞近乎逐字重合"的重复条目，保留价值序靠前的。

    [2026-09-14 新增] 真实 API 复验产出里第 3、4 条都是 OpenAI 上市/控速：
      · OpenAI CEO支持控制AI发展速度，强调非停止技术进步（投融资）
      · OpenAI年内不上市，CEO称2026年上市不妥（投融资）
    两条占了 8 条里的 2 个名额，等于当天少报一条别的新闻。

    [2026-09-14 阈值重新校准] 原判据"共同 2-gram ≥5"太松，会误杀真新闻。
    用真实标题实测（共享主体数 / 共同词片 / Jaccard）：
      · 真重复·同一事件措辞几乎一致：19 词片 0.83、23 词片 0.82  → 必须去掉
      · 易误伤·同一公司不同事件：    7 词片 0.22、 7 词片 0.17  → 必须保留
        （「宇树科技发布人形机器人G1+」与「宇树科技科创板挂牌」是两条不同新闻；
          「OpenAI 上市时机」与「OpenAI 支持控制 AI 发展速度」也是两件事）
    两者在"共同词片"上都是 7，根本分不开；Jaccard 却把它们拉得很开
    （0.82 vs 0.22）。真实复验 run 34821688274 里 dedupe 一口气砍掉 9 条中的 3 条，
    最终只剩 6 条 —— 丢真新闻的代价远大于偶尔留下一条相近话题，
    所以阈值收到"近乎逐字重复"这一档，宁松勿狠。
    """
    out = []
    for it in items:
        e, g = _title_shape(it.get("title", ""))
        dup = None
        for k in out:
            if k["cat"] != it["cat"]:
                continue                 # 不同类别的同主体新闻（如芯片 vs 财报）不算重复
            if k.get("region", "cn") != it.get("region", "cn"):
                continue                 # 分属两个板块，各自保留（配额本就分开算）
            ke, kg = _title_shape(k.get("title", ""))
            shared_ent = e & ke
            if not shared_ent:
                continue
            union = len(g | kg)
            jac = len(g & kg) / union if union else 0.0
            if jac >= min_jaccard:
                dup = (k["title"], len(shared_ent), jac)
                break
        if dup:
            log(f"  丢弃话题重复的条目（与「{dup[0][:16]}」共享主体 {dup[1]} 个、"
                f"重合度 {dup[2]:.2f}）: {it['title'][:20]}")
            continue
        out.append(it)
    return out


def select_balanced(cands, target=MAX_ITEMS, prefer=MIN_PER_CELL):
    """从候选里挑 target 条，按"地域 × 维度"八格尽量均衡；返回结果保持原价值序。

    [2026-09-14 新增] 之前直接把候选全量写入，模型倾向多写"产业动态"、
    少写"投融资"，四类就不均衡。这里改成"轮转取用"：
      第一轮：每格先各取 1 条（8 格都出现）
      第二轮：每格再各取 1 条（每格凑到 2 条，共 16 条）
      剩余名额：按候选原本的价值降序补齐

    [2026-09-21 升级为八格] 页面拆成国内/国外两个板块后，均衡的粒度从一个
    维度变成两个：不只是"四类各有"，还要"国内国外各有四类"。
    若某格当日确实没有候选，名额自动让给其它格（宁少勿滥，不硬塞错标条目）。
    """
    if len(cands) <= target:
        _log_cell_dist("均衡选取（候选未超上限）", cands)
        return cands
    by_cell = {}
    for idx, c in enumerate(cands):
        by_cell.setdefault((c.get("region", "cn"), c["cat"]), []).append(idx)
    picked = set()
    for want in range(1, prefer + 1):
        for region in REGIONS:
            for cat in CAT_LABELS:
                pool = by_cell.get((region, cat), [])
                if len(picked) >= target:
                    break
                if len(pool) >= want:
                    picked.add(pool[want - 1])
    for idx in range(len(cands)):          # 剩余名额按价值序补齐
        if len(picked) >= target:
            break
        picked.add(idx)
    out = [cands[i] for i in sorted(picked)]
    _log_cell_dist(f"均衡选取：候选 {len(cands)} 条 → 写入 {len(out)} 条", out)
    return out


def _cell_dist(items):
    """按 (region, cat) 统计条数，返回 {region: {cat: n}}。"""
    d = {r: Counter() for r in REGIONS}
    for it in items:
        d.setdefault(it.get("region", "cn"), Counter())[it["cat"]] += 1
    return d


def _log_cell_dist(prefix, items):
    """打印 8 格分布。这一行是排查"某板块缺某一维度"的关键分界。"""
    d = _cell_dist(items)
    parts = []
    for r in REGIONS:
        parts.append(f"[{REGION_LABELS[r]} {sum(d[r].values())}条] "
                     + " ".join(f"{CAT_LABELS[k]}{d[r].get(k, 0)}" for k in CAT_LABELS))
    log(f"  {prefix} " + "  ".join(parts))


def _filter_candidates(raw_items, by_url, seen=None, region="cn"):
    """对阶段 A 的原始输出做全套硬过滤，返回候选列表（不含 _rank）。

    [2026-09-14 抽出] 这段原先内联在 generate_news 里。抽出来是为了让
    collect_candidates() 能在"某类候选不足"时再跑一轮选题并合并结果，
    两轮共用同一套过滤规则——否则补选进来的条目会绕过校验。

    [2026-09-21 增加 region] 阶段 A 现在是**按地域分批调用**的，
    候选的地域直接取该批传进来的 region（与素材池的划分一致），
    不再听模型自己的判断——模型的 region 语感不稳，会把国内部委发文说成国外。
    唯一例外：材料本身是"中国政府网"来的，强制为国内。

    过滤项：分类合法性 / URL 白名单 / 重复素材 / 标题长度上下界 /
    标题未翻译 / 标题含提示词文字 / 聚合与消费电子类。
    """
    cands = []
    if seen is None:
        seen = set()
    for it in raw_items:
        cat = str(it.get("cat", "")).strip().lower()
        if cat not in CAT_LABELS:
            continue
        url = str(it.get("url", "")).strip()
        if url not in by_url:          # URL 白名单强校验
            log(f"  丢弃编造 URL 的条目: {it.get('title', '')[:30]} url={url}")
            continue
        if url in seen:                # 同一条素材重复出现在两条候选里
            log(f"  丢弃重复素材的条目: {it.get('title', '')[:30]}")
            continue
        seen.add(url)
        title = clean_for_js(it.get("title", ""))[:60]
        if not title:
            continue
        # 8 月定版标准：标题 20-32 字（9 月实测出现过 8 字标题），低于下限直接丢弃
        if len(title) < MIN_TITLE_LEN:
            log(f"  丢弃标题过短的条目（{len(title)}字）: {title}")
            continue
        # 上界：实测出现过 52 字的失控长标题，超过 40 字说明没有压缩过
        if len(title) > MAX_TITLE_LEN:
            log(f"  丢弃标题过长的条目（{len(title)}字）: {title[:30]}")
            continue
        # 英文原句未翻译（如 "Rapidly scaling online storage"）→ 阶段 A 就筛掉
        if title_english_residue(title):
            log(f"  丢弃标题未翻译的条目: {title[:30]}")
            continue
        # 标题里混进了提示词文字（模型偶尔把写作要求当标题输出）
        if prompt_leak(title):
            log(f"  丢弃标题混入提示词的条目: {title[:30]}")
            continue
        # 聚合类 / 消费电子类标题
        if title_blocked(title):
            log(f"  丢弃聚合或消费电子类条目: {title[:30]}")
            continue
        m = by_url[url]
        # 地域：官方政策原文必为国内；其余以本批的 region 为准
        reg = "cn" if (m.get("source") == "中国政府网"
                       or "gov.cn" in str(m.get("url", ""))) else region
        cands.append({
            "cat": cat,
            "region": reg,
            "title": title,
            "source": clean_for_js(it.get("source", ""))[:30] or m["source"],
            "url": url,
        })
    return cands


def _cell_ready(items, per_cell):
    """8 个（地域 × 维度）格子是否都写够了 per_cell 条（阶段 B 提前收工的判据）。"""
    d = _cell_dist(items)
    return all(d[r].get(k, 0) >= per_cell for r in REGIONS for k in CAT_LABELS)


def _cells_lack(items, per_cell, label_fn=None):
    """返回还没达标的格子标签列表，用于日志与补选提示。"""
    d = _cell_dist(items)
    out = []
    for r in REGIONS:
        for k in CAT_LABELS:
            if d[r].get(k, 0) < per_cell:
                out.append(f"{REGION_LABELS[r]}·{CAT_LABELS[k]}")
    return out


def collect_candidates(region_mat, by_url, today_cn, region, attempt=0):
    """阶段 A：**针对单个地域**选题 + 硬过滤；该地域某类候选不足就补选并合并。

    返回 (摘要对象, 候选列表)。

    [2026-09-14 新增补选] 真实 API 复验（run 34819377914）产出 8 条、标题均
    25.5 字、正文均 134.2 字，长度全部达标，四类却是
    {政策发布:3, 技术突破:2, 投融资:3}，"产业动态"一条都没有。根因是
    select_balanced 只能从候选里挑，变不出候选里没有的类别 —— 所以必须
    在候选阶段就把每一类的下限卡住，而不是等最后才发现缺项。
    补选时 temperature 提到 0.7，逼模型换个角度去素材里找该类新闻。

    [2026-09-21 按地域分批] region_mat 是该地域专属的素材池
    （见 split_material_by_region），补选只在这个池子里翻，
    不会"越界"去挑另一个地域的新闻。
    """
    obj, cands = {}, []
    if not region_mat:
        log(f"  阶段 A【{REGION_LABELS[region]}】素材池为空，跳过选题")
        return obj, cands
    for rnd in range(MAX_SELECT_ROUNDS):
        try:
            # 首轮 0.4 保稳；补选轮 0.7 求变，否则模型每次挑的都差不多
            # 补选轮还要把已选素材列进提示词——不列的话模型会把上一轮原样再抄一遍，
            # 补选轮输出的条目会被 _filter_candidates 当"重复素材"全部丢掉。
            raw = call_glm(
                build_select_prompt(region_mat, today_cn, region,
                                    attempt=attempt + rnd,
                                    used=[(c["title"], c["url"]) for c in cands]
                                    if rnd else None),
                temperature=0.4 if rnd == 0 else 0.7,
            )
        except Exception as e:
            log(f"  阶段 A【{REGION_LABELS[region]}】第{rnd+1}轮调用失败: {e}")
            continue
        got = parse_llm_json(raw)
        if not isinstance(got, dict):
            continue
        if not obj:
            obj = got                  # 首轮成功的 summary 作为最终摘要
        fresh = _filter_candidates(got.get("items", []) or [],
                                   by_url, {c["url"] for c in cands},
                                   region=region)
        if fresh:
            base = len(cands)          # 先到的轮次价值序更靠前
            for k, c in enumerate(fresh):
                c["_rank"] = base + k
            cands.extend(fresh)
        dist = Counter(c["cat"] for c in cands)
        lack = [CAT_LABELS[k] for k in CAT_LABELS
                if dist.get(k, 0) < MIN_CANDS_PER_CELL]
        log(f"  阶段 A【{REGION_LABELS[region]}】第{rnd+1}轮：候选 {len(cands)} 条 "
            + " ".join(f"{CAT_LABELS[k]}{dist.get(k, 0)}" for k in CAT_LABELS)
            + (f"｜仍缺 {'/'.join(lack)}" if lack else "｜四类齐备"))
        if not lack:
            break
    return obj, cands


def generate_news(material, today_cn, attempt=0):
    """两阶段生成：先选题（阶段 A，**按地域分两次**），再逐条撰写正文（阶段 B）。

    [2026-09-14] 由"一次生成全部条目"改为两阶段。原因见 build_select_prompt /
    build_desc_prompt 的注释：免费模型 glm-4-flash 在批量任务里会把每条正文
    压缩到 50 字上下，达不到 8 月定版的 100-160 字标准；拆窄任务后才写得长。

    [2026-09-21] 目标改为"国内 8 条 + 国外 8 条"。阶段 A 先把素材池按地域劈开，
    每个地域单独跑一轮选题（各自 4 类 × 4 条候选），合并后统一进入阶段 B。
    阶段 B 仍逐条写，但轮转键升级成 (region, cat)，保证早收工时两个板块都齐。
    """
    by_url = {m["url"]: m for m in material}
    # [2026-09-14] 数字溯源必须把正文摘要一起纳入。
    # 否则会出现反向 bug：模型按 8 月标准写出"发行价150.80元每股"这类
    # 取自摘要的真实数字，却因标题里没有而被判为"编造数字"整条丢弃。
    material_text = " ".join(
        (m.get("title", "") + " " + (m.get("summary") or "")) for m in material
    )

    # —— 阶段 A：按地域分两次选题 + 硬过滤（含候选不足时的补选）——
    pools = split_material_by_region(material)
    log("  素材地域拆分：" + "  ".join(
        f"{REGION_LABELS[r]}池 {len(pools[r])} 条" for r in REGIONS))
    cands, summaries = [], {}
    for region in REGIONS:
        obj, got = collect_candidates(pools[region], by_url, today_cn,
                                      region, attempt=attempt)
        s = clean_for_js(obj.get("summary", ""))[:70] if isinstance(obj, dict) else ""
        if s:
            summaries[region] = s
        base = len(cands)
        for k, c in enumerate(got):        # 先国内后国外，价值序不跨地域比较
            c["_rank"] = base + k
        cands.extend(got)
    _log_cell_dist("阶段 A 候选汇总", cands)

    # —— 阶段 B：逐条素材单独撰写 110-150 字正文 ——
    # [2026-09-14] 由"一次不成即丢弃"改为"定向重写至多 3 轮"。
    # 实测（run 34818610548）10 条候选最终只剩 3 条，其中 4 条死于
    # "数字不可核实"、2 条死于"英文残留"——这些都是重写一次就能修好的问题，
    # 直接丢弃等于每天白扔 5-6 条可用新闻。现在把体检结果（desc_issues）
    # 原样回灌进提示词，指名要求改掉那个数字/那句英文。
    log(f"  选题完成：{len(cands)} 条候选，开始逐条撰写正文"
        f"（8 格各满 {MIN_PER_CELL_DESC} 条才收工，上限 {DESC_TARGET} 条）")
    # 按 (地域 × 维度) 轮转排序，保证无论写到哪里停手，两个板块四个维度都齐
    # prefer 取 MIN_PER_CELL_DESC：先给每个格子凑满收工所需的条数，
    # 再按原价值序补。这样"提前收工"时刚好写满 16 条，不多烧 4-7 次 API。
    cands = interleave_by_cell(cands, prefer=MIN_PER_CELL_DESC)
    out = []
    for ci, c in enumerate(cands):
        if len(out) >= DESC_TARGET:
            log(f"  已达上限 {DESC_TARGET} 条，其余候选不再调用")
            break
        # 收工条件不只是"凑够 16 条"，还要 8 格每格都够 2 条供均衡选取。
        # [2026-09-14] 复验时只按条数收工，结果 8 条里缺了"产业动态"——
        # 因为写到 12 条就停手，而 industry 候选恰好都排在后面还没轮到。
        if len(out) >= MAX_ITEMS and _cell_ready(out, MIN_PER_CELL_DESC):
            log(f"  已写满 {len(out)} 条且 8 格齐备，其余候选不再调用")
            break
        if ci:
            # 阶段 B 调用密集（每天 16-30 次），主动留出间隔，
            # 比撞上限流再退避更省时间也更容易成功
            time.sleep(1.0)
        m = by_url[c["url"]]
        desc, issues, got_any = "", {}, False
        # 至多 3 轮：首轮通用提示，之后按体检结果定向修复
        for b in range(3):
            try:
                got = call_glm(
                    build_desc_prompt(m, attempt=b, fix=issues if b else None),
                    temperature=0.5 if b == 0 else 0.35,
                )
            except Exception as e:
                log(f"  正文生成调用失败（{c['title'][:20]}）: {e}")
                continue
            got_any = True
            cand_desc = clean_for_js(clean_desc_output(got))[:400]
            if not cand_desc:
                continue
            desc = cand_desc
            issues = desc_issues(desc, material_text)
            if not issues:
                break
            log(f"  正文待修（第{b+1}轮）{issues_brief(issues)}: {c['title'][:22]}")
        if not got_any:
            log(f"  丢弃调用全失败的条目: {c['title'][:26]}")
            continue
        # 三轮都没救回来 → 才是真正不可用
        if issues:
            log(f"  丢弃三轮重写仍不合格的条目: {c['title'][:26]}")
            continue
        # 标题侧的数字溯源是最后一道保险（标题由阶段 A 产出，此前只查了长度）
        if ungrounded_numbers(c["title"], material_text):
            log(f"  丢弃标题数字不可核实的条目: {c['title'][:30]}")
            continue
        # 分类确定性纠偏（模型常为"四类均衡"而错标）。
        # [2026-09-21] 必须带上地域——"政策发布"在两个板块的判据不同：
        # 国内只认中国官方发文，国外只认境外政府/监管机构的正式规则。
        fixed = normalize_category(c["cat"], c["title"], desc, c.get("region", "cn"))
        if fixed != c["cat"]:
            log(f"  分类纠偏: {REGION_LABELS[c.get('region', 'cn')]} "
                f"{c['cat']}→{fixed}  {c['title'][:24]}")
        out.append({
            "cat": fixed,
            "region": c.get("region", "cn"),
            "catLabel": CAT_LABELS[fixed],
            "title": c["title"],
            "desc": desc,
            "source": c["source"],
            "url": c["url"],
            "_rank": c.get("_rank", 999),
        })
    # 各格实际写出多少条合格正文。这一行是排查"缺项"的关键分界：
    # 若某格候选有 3 条却没写出来 → 是正文体检门槛把它筛掉了（改门槛）；
    # 若某格候选本来就不足 3 条 → 是阶段 A/补选的问题（改提示词）。
    # [2026-09-14] 复验缺"产业动态"时，只看最终 8 条根本分不清是哪种，
    # 只能翻后台日志逐条数，故把这一步固化成常规输出。
    _log_cell_dist("阶段 B 合格正文", out)
    # 候选 → 最终 16 条：按八格均衡选取（必须在 summary 校验之前，
    # 否则摘要可能提及被裁掉的条目）
    # 先按阶段 A 的价值序还原（阶段 B 为了覆盖 8 格做了轮转排序）
    out.sort(key=lambda x: x.get("_rank", 999))
    for x in out:
        x.pop("_rank", None)
    # 去掉话题重复的（同一主体 + 同地域同类 + 措辞重合），再做均衡选取
    out = dedupe_similar(out)
    out = select_balanced(out)
    # 摘要：两个地域各写了一句，拼成"【国内】… 【国外】…"便于在日卡头部一眼看全
    parts = [f"【{REGION_LABELS[r]}】{summaries[r]}" for r in REGIONS if summaries.get(r)]
    summary = clean_for_js("　".join(parts))[:160]
    if out and not summary_consistent(summary, out):
        log("  摘要提及了未收录内容，改用条目标题兜底摘要")
        summary = clean_for_js(fallback_summary(out))
    return summary, out
# ---------------------------------------------------------------------------
# HTML 写入
# ---------------------------------------------------------------------------
def js_literal(items, date_str, weekday, summary):
    """构造一段可插入 NEWS_DATA 的 JS 对象文本。

    [2026-09-21] 新增 region 字段与顶层 regioned 标记。页面靠 regioned 判断
    "这一天是否已按国内外双板块排版"——历史数据补齐 region 之前，
    老日期会退回单列表渲染，不会因为缺字段而丢内容。
    """
    item_lines = []
    for i in items:
        item_lines.append(
            f'      {{ region: "{i.get("region", "cn")}", '
            f'cat: "{i["cat"]}", catLabel: "{i["catLabel"]}", '
            f'title: "{i["title"]}", desc: "{i["desc"]}", '
            f'source: "{i["source"]}", url: "{i["url"]}" }}'
        )
    block = (
        "  {\n"
        f'    date: "{date_str}",\n'
        f'    weekday: "{weekday}",\n'
        "    regioned: true,\n"
        f'    summary: "{summary}",\n'
        "    items: [\n"
        + ",\n".join(item_lines) +
        "\n    ]\n"
        "  },"
    )
    return block


def write_into_html(html_path, block, date_str):
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    if f'date: "{date_str}"' in content:
        log(f"  {html_path} 已含 {date_str}，跳过写入")
        return False

    anchor = "var NEWS_DATA = [\n"
    idx = content.find(anchor)
    if idx == -1:
        raise RuntimeError(f"{html_path} 中未找到 NEWS_DATA 锚点")
    insert_at = idx + len(anchor)
    new_content = content[:insert_at] + block + "\n" + content[insert_at:]
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    log(f"  已写入 {html_path}（{date_str}，{len(block)} 字符）")
    return True


def check_js_syntax(html_path):
    """用 node 校验 NEWS_DATA 语法。

    返回 True=通过 / False=不通过 / None=node 不可用（跳过校验）。
    """
    js_check = (
        "const s=require('fs').readFileSync(process.argv[1],'utf8');"
        "const m=s.match(/var NEWS_DATA = (\\[[\\s\\S]*?\\]);/);"
        "if(!m){console.error('未找到 NEWS_DATA');process.exit(1)}"
        "JSON.parse(JSON.stringify(eval('('+m[1]+')')));"
        "console.log('OK')"
    )
    code = (
        "const cp=require('child_process');"
        f"const r=cp.spawnSync('node',['-e',{json.dumps(js_check)},process.argv[1]],"
        "{encoding:'utf8'});process.stdout.write(r.stdout);process.stderr.write(r.stderr);"
        "process.exit(r.status||0)"
    )
    try:
        proc = subprocess_run(code, html_path)
    except FileNotFoundError:
        print("  node 不可用，改用 Python 兜底校验")
        return check_news_data_python(html_path)
    return proc == 0


def check_news_data_python(html_path):
    """不依赖 node 的兜底校验：把 NEWS_DATA 的 JS 字面量还原为 JSON 再解析。

    做法：先把字符串字面量抽成占位符（避免误改字符串内部内容），
    再给裸键补引号，最后还原字面量并用 json 解析。
    """
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"var NEWS_DATA = (\[[\s\S]*?\]);", content)
    if not m:
        print(f"  [兜底校验] 未找到 NEWS_DATA: {html_path}")
        return False
    src = m.group(1)

    holder = []

    def _stash(mm):
        holder.append(mm.group(0))
        return f"\x00{len(holder) - 1}\x00"

    safe = re.sub(r'"(?:[^"\\]|\\.)*"', _stash, src)
    safe = re.sub(r"([{,])(\s*)([A-Za-z_]\w*)(\s*):", r'\1\2"\3"\4:', safe)
    safe = re.sub(r"\x00(\d+)\x00", lambda mm: holder[int(mm.group(1))], safe)
    try:
        json.loads(safe)
    except Exception as e:
        print(f"  [兜底校验] NEWS_DATA 解析失败: {e}")
        return False
    print("  [兜底校验] OK")
    return True


def subprocess_run(code, arg):
    import subprocess
    r = subprocess.run(
        ["node", "-e", code, arg],
        capture_output=True, text=True, timeout=60,
    )
    print(r.stdout, end="")
    if r.returncode != 0:
        print(r.stderr, end="")
    return r.returncode


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    today_cn = f"{today.year}年{today.month}月{today.day}日"
    weekday = WEEKDAYS[today.weekday()]
    log(f"开始每日 AI 产业动态流水线：{today_str} {weekday}")

    # 0. 关键前置检查
    if not ZHIPU_API_KEY:
        log("缺少 ZHIPU_API_KEY，无法生成高质量条目（不写入，留待配置后重试）")
        return 2
    for p in (FULL_HTML, LITE_HTML):
        if not os.path.exists(p):
            log(f"缺少源文件: {p}")
            return 2

    # 0.5 幂等前置检查（2026-09-18 新增）
    # 原先"当天已写入"是在最末尾写文件时才发现的——那之前已经白烧 30-45 次
    # LLM 调用。而每天失败率不低（真实 API 复验常见"模型当天顽抗、条数不达标"），
    # 所以现在给 workflow 加了第二个触发时段做自动补跑；没有这个前置检查，
    # 补跑那一趟会把当天额度再烧一遍，反而更容易撞上智谱限流。
    # 判据：两个文件都已含当天日期块 → 直接退出，不抓素材、不调模型。
    already = []
    for p in (FULL_HTML, LITE_HTML):
        try:
            with open(p, encoding="utf-8") as f:
                if f'date: "{today_str}"' in f.read():
                    already.append(os.path.basename(p))
        except Exception:
            continue
    if len(already) == len((FULL_HTML, LITE_HTML)):
        log(f"两个文件均已含 {today_str} 的条目（{', '.join(already)}），"
            "跳过抓取与生成（幂等）")
        return 0

    # 1. 抓素材
    material = collect_material(hours=72)
    if len(material) < 12:
        log("素材不足（<12 条），本次跳过，避免生成低质/编造内容")
        return 0
    # 两个板块各有素材才可能各出 8 条。任一侧为空就说明当天素材严重偏科，
    # 与其写出"国内 16 条 / 国外 0 条"，不如不写。
    _pools = split_material_by_region(material)
    if min(len(_pools[r]) for r in REGIONS) < 6:
        log("素材地域偏科（国内/国外任一池 <6 条），本次跳过："
            + " ".join(f"{REGION_LABELS[r]}{len(_pools[r])}" for r in REGIONS))
        return 0

    # 2. LLM 生成：目标 16 条（国内 8 + 国外 8），单次不足自动重试一次（提高 temperature 扩选题）
    summary, items = "", []
    last_err = None
    for attempt in range(2):
        try:
            summary, items = generate_news(material, today_cn, attempt=attempt)
            log(f"第{attempt+1}次生成 {len(items)} 条（target={MAX_ITEMS}）")
        except Exception as e:
            last_err = e
            log(f"第{attempt+1}次 LLM 生成失败: {e}")
            continue
        if len(items) >= MAX_ITEMS:
            break

    if not items:
        log(f"两轮生成均失败（最后错误: {last_err}），放弃本次写入")
        return 1
    # 硬底线按"两个板块各 6 条"设：低于此说明当天素材或模型状态明显异常，
    # 写上去等于把一个残缺的页面推给用户，不如留线上旧版。
    _n_cn = sum(1 for x in items if x.get("region") == "cn")
    _n_intl = len(items) - _n_cn
    if min(_n_cn, _n_intl) < 6:
        log(f"两个板块产出失衡（国内 {_n_cn} 条 / 国外 {_n_intl} 条，"
            "任一板块 <6 条即视为失败），放弃本次写入")
        return 1
    if len(items) < MAX_ITEMS:
        log(f"仅生成 {len(items)} 条（<{MAX_ITEMS} 条），仍写入但内容偏少")
    _log_cell_dist("最终写入", items)
    # 3. 构造 JS 块并写入两文件
    block = js_literal(items, today_str, weekday, summary)
    changed = False
    for p in (FULL_HTML, LITE_HTML):
        try:
            changed |= write_into_html(p, block, today_str)
        except Exception as e:
            log(f"写入失败 {p}: {e}")
            return 1
    if not changed:
        log("两个文件当天均已写入过，无需更新")
        return 0

    # 4. JS 语法校验（True=通过 / False=不通过 / None=node 不可用则跳过）
    ok = True
    for p in (FULL_HTML, LITE_HTML):
        res = check_js_syntax(p)
        if res is False:
            ok = False
            log(f"JS 语法校验失败: {p}")
        elif res is None:
            log(f"  node 不可用，跳过 {p} 的语法校验")
    if not ok:
        log("语法校验未通过，请人工检查（git 未提交，线上不受影响）")
        return 1

    # 5. 落一个状态文件供 workflow 判断是否提交
    with open(os.path.join(WORKSPACE, ".last_update"), "w", encoding="utf-8") as f:
        f.write(f"{today_str} {weekday} items={len(items)}\n")
    log(f"完成：{today_str} 写入 {len(items)} 条到完整版+阉割版，等待提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
