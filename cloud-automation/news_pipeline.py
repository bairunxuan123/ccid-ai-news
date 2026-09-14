#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 产业动态每日自动流水线（云端版，由 GitHub Actions 每天 14:00 触发）

职责：
  1. 抓取多个公开新闻源（RSS）最近 24h 的 AI 相关条目
  2. 调用智谱 GLM API，把素材整理成 8 条"四类均衡"产业动态
     （政策发布 policy / 技术突破 tech / 产业动态 industry / 投融资 capital）
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
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

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
RSS_SOURCES = [
    "https://www.ithome.com/rss/",                                  # 中文 IT 综合
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",  # Atom
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "https://openai.com/news/rss.xml",
]

# AI 相关性过滤（强命中 / 弱命中+排除词，英文按词边界，避免 email/said 误伤）
STRONG_EN = [
    "openai", "anthropic", "chatgpt", "gemini", "claude", "llama", "deepseek",
    "mistral", "nvidia", "copilot", "llm", "hbm", "token", "machine learning",
    "neural", "transformer", "generative ai", "robotaxi", "agentic",
    "grok", "xai", "qwen", "midjourney", "stable diffusion", "hugging face",
    "artificial intelligence", "foundation model", "gpu", "datacenter",
]
STRONG_CN = [
    "人工智能", "大模型", "大语言模型", "智能体", "多模态", "自动驾驶", "智驾",
    "算力", "数据中心", "英伟达", "深度学习", "机器学习", "神经网络", "生成式",
    "大模型公司", "ai大模型", "具身智能", "智算", "aigc", "ai服务器", "ai芯片",
    "ai应用", "ai安全", "ai治理", "ai眼镜", "ai手机", "ai pc",
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
TITLE_BLOCK = [
    "早报", "日报", "晚报", "周报", "月报", "盘点", "汇总", "速览", "一周要闻",
    "时事", "寻求胜利", "官图", "试驾", "新车", "座舱", "续航", "渲染图",
    "ios", "iphone", "ipad", "macbook", "airpods", "apple watch", "鸿蒙",
    "扫拖", "扫地机器人", "门锁", "浴霸", "晾衣机", "加湿器",
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


def risky_tokens(text):
    """把金额/百分比/倍数统一换算成可跨中英文比对的规范 token。

    素材多为英文（"$500M"），生成结果是中文（"5亿美元"），
    直接做子串匹配会误杀，因此统一折算为绝对数值再比对。
    同时对金额额外发放 "mag:" 别名，使 "$2B" 能匹配中文的"20亿"（未带币种）。
    """
    if not text:
        return set()
    raw = text
    s = re.sub(r"\s+", "", text)
    toks = set()

    def money(n):
        toks.add("amt:%d" % round(n))
        toks.add("mag:%d" % round(n))

    # 中文口径金额：5亿 / 5亿美元 / 500万美元 / 2000万元
    for m in re.finditer(r"(\d+(?:\.\d+)?)(亿|万|千)?(美元|美金|元|人民币|港币)", s):
        num = float(m.group(1))
        mult = {"亿": 1e8, "万": 1e4, "千": 1e3}.get(m.group(2), 1.0)
        money(num * mult)
    # 中文裸量级（无币种）：20亿 / 500万
    for m in re.finditer(r"(\d+(?:\.\d+)?)(亿|万|千)(?![美元人民币港])", s):
        num = float(m.group(1))
        mult = {"亿": 1e8, "万": 1e4, "千": 1e3}[m.group(2)]
        toks.add("mag:%d" % round(num * mult))
    # 英文口径金额：$500M / 2 billion / 500M
    # 必须在原文本（保留空格）上匹配：去空格会把 "M valuation" 粘成 "Mvaluation"，
    # 词边界失效导致英文金额整体漏检。
    for m in re.finditer(_EN_MONEY_RE.pattern, raw, re.I):
        num = float(m.group(1))
        money(num * _MAG[m.group(2).lower()])
    # 百分比
    for m in re.finditer(r"(\d+(?:\.\d+)?)%", s):
        toks.add("pct:%g" % float(m.group(1)))
    # 倍数
    for m in re.finditer(r"(\d+(?:\.\d+)?)倍", s):
        toks.add("x:%g" % float(m.group(1)))
    return toks


def numbers_grounded(text, material_text):
    """文本里的金额/百分比/倍数必须在素材中真实出现过，否则视为不可核实。

    素材只有标题、没有正文，LLM 一旦"推算"金额或估值就会失真（例如
    "估值约2万亿美元"），因此这类数字必须能被素材标题支持。
    比对时统一折算口径，避免 "$500M" 与 "5亿美元" 被误判为不匹配。
    """
    toks = risky_tokens(text)
    if not toks:
        return True
    return toks <= risky_tokens(material_text)


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
    """统计文本中"不应残留"的英文单词数（长度≥4 且非专有名词白名单）"""
    if not text:
        return 0
    n = 0
    for w in re.findall(r"[A-Za-z]{4,}", text):
        if w.lower() not in _EN_ALLOW:
            n += 1
    return n


# 聚合类 / 消费电子类标题拦截（即使含 AI 字样也不属于"AI 产业动态"）
TITLE_BLOCK = [
    "早报", "日报", "晚报", "周报", "月报", "盘点", "汇总", "速览", "一周要闻",
    "时事", "寻求胜利", "官图", "试驾", "新车", "座舱", "续航", "渲染图",
    "ios", "iphone", "ipad", "macbook", "airpods", "apple watch", "鸿蒙",
    "扫拖", "扫地机器人", "门锁", "浴霸", "晾衣机", "加湿器",
]


def title_blocked(title):
    """标题是否为应当排除的类型（聚合汇总 / 消费电子 / 汽车新品）"""
    t = (title or "").lower()
    return any(b in t for b in TITLE_BLOCK)

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


def fetch_rss_items(source):
    """抓取单个 RSS/Atom 源，返回 [{title, url, source, published}]"""
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

    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = first_text(node, "title")
        link = first_link(node)
        pub = first_text(node, "pubDate") or first_text(node, "published") or first_text(node, "updated")
        if not title or not link:
            continue
        items.append({"title": title, "url": link, "source": source, "published": pub})
    return items


def collect_material(hours=48):
    """汇总所有源最近 N 小时且 AI 相关的条目，去重。

    默认 48h 窗口（原 36h），单源上限 60 条（原 40），保证 8 条生成有足够素材池。
    """
    seen, material = set(), []
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    for src in RSS_SOURCES:
        for it in fetch_rss_items(src):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            if not is_ai_related(it["title"]):
                continue
            material.append(it)
        if len(material) >= 60:
            break
    log(f"素材汇总：{len(material)} 条 AI 相关（近 {hours}h）")
    return material


# ---------------------------------------------------------------------------
# LLM 生成
# ---------------------------------------------------------------------------
def call_glm(prompt, temperature=0.4):
    body = json.dumps({
        "model": ZHIPU_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        ZHIPU_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ZHIPU_API_KEY}",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def build_prompt(material, today_cn, attempt=0):
    lines = "\n".join(
        f"{i+1}. {m['title']} ｜来源:{m['source']} ｜URL:{m['url']}"
        for i, m in enumerate(material[:40])
    )
    # 第二次重试时调高 temperature，增加多样性
    temp_note = "" if attempt == 0 else "（上一轮素材较少或输出条目不足，请尽可能扩大选题范围，放宽到 AI 邻域事件如芯片、算力、机器人、自动驾驶、数据中心等）"
    return f"""今天是{today_cn}。下面是从各大科技媒体抓取的 AI 相关新闻素材（编号+标题+URL）。{temp_note}

素材：
{lines}

请从中挑选当日最有产业价值的 AI 新闻，整理成"人工智能产业动态"。

**目标数量：8 条，覆盖 4 类各 2 条**：
- policy 政策发布 2 条：政府部门、监管机构、行业标准、法律法规相关
- tech 技术突破 2 条：模型/算法/芯片/算力/产品技术本身的进展
- industry 产业动态 2 条：企业合作、产品上市、产能布局、行业趋势、企业业绩
- capital 投融资 2 条：融资、并购、IPO、估值变化

分类规则细节：
- 8 条总数是硬要求；但若某一类当日确实没有对应新闻（极少），该类允许为 1 条，其它类补足 8 条总数；
- 若实在凑不齐 8 条高质量新闻，可放宽到 6-7 条，但不得少于 6 条；
- 宁可少几条也绝不硬塞与 AI 产业无关的内容（消费电子/汽车新品/系统更新/政治人物等）——系统会硬过滤拦截，但需要你自觉避开。

分类口径（必须严格按新闻实质判断，宁缺勿错）：
- policy 政策发布：政府部门、监管机构、行业标准、法律法规相关
- tech 技术突破：模型/算法/芯片/算力/产品技术本身的进展
- industry 产业动态：企业合作、产品上市、产能布局、行业趋势、企业业绩
- capital 投融资：融资、并购、IPO、估值变化

硬性要求：
1. 只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记。
2. 对象格式严格为：
{{"summary":"一句话概括今日AI产业要点，不超过80字","items":[{{"cat":"policy","title":"标题不超过30字","desc":"简述80-120字，客观专业","source":"媒体名","url":"https://原文链接"}},...]}}
3. source 填媒体简称（如 IT之家、TechCrunch、The Verge），url 必须从上方素材中挑选真实 URL，禁止编造、拼接或改写。
4. title 用中文，控制在 30 字内，须是新闻事实的准确概括，不要加评价性形容词；desc 用中文书面语客观陈述，不要口语和感叹号。
5. **不要为了凑齐"每类 2 条"而错标分类**。若某一类当日实在没有对应新闻（极少），该类可以为 1 条，但其它类补足 8 条总数；不要硬塞错标条目充数。错标分类比数量不均衡严重得多。
6. 输出前逐条自查：这条新闻的实质与所标分类是否一致？不一致就改正分类或换掉该条。
7. **绝对排除**与 AI 产业无关的内容：消费电子新品（手机/相机/耳机/显示器/笔记本）、汽车新车与试驾（含 MPV/SUV 官图）、灯光与外设软件、操作系统更新（Windows/iOS/安卓的系统或功能更新）、产品与发布会预告、游戏影视娱乐、体育赛事、社会新闻——素材里出现也不要选。
8. 选题限于产业与技术范畴：判断标准是"这条新闻是否直接反映 AI 产业或技术本身的变化"。凡属个人公开表态、社会活动、与产业无关的公共事务，一律不选。
9. 素材只有标题（没有正文），因此 title 与 desc 中**不得出现素材里没有的金额、估值、百分比、增长倍数**（如"50亿美元""2万亿美元""增长70%"）。需要表达程度时改用定性描述（如"大幅增长""估值处于高位"）。系统会校验并丢弃含无法核实数字的条目。
10. **标题必须忠实于原文事实**：素材多为英文，须准确理解后再译为中文，不得截取英文原句、不得把原文没有的判断归纳进标题。例如原文讲"为 AI 供电是架构问题"，就不能写成"AI 在音频内容中的应用"。
11. **desc 必须全部使用中文**（OpenAI、ChatGPT 等专有名词除外），不得残留英文句子或英文短语。系统会校验并丢弃英文残留过多的条目。
12. 不要选用"早报/日报/盘点/汇总/速览"这类聚合内容，也不要选消费电子（iOS/iPhone/手机/相机/耳机）与汽车新品——素材里出现也不要选。
13. **标题不得泛化**：必须保留原文的核心主体与事件（谁做了什么），禁止写成"OpenAI寻求技术突破""某公司面临挑战"这类丢掉具体信息的空泛标题。原文若讲的是具体的竞赛、事件、人物加入、计划，就如实写出。
14. **summary 只能概括本次 items 里实际收录的条目**，不得提及未收录的新闻。系统会核对，出现未收录内容视为错误。
15. 分类补充口径：企业发生安全事故、被攻击、被罚款等负面事件属于"产业动态"，不要标成"技术突破"；只有当新闻本身是技术能力/模型能力的进展时才用"技术突破"。"""


def parse_llm_json(content):
    """从 LLM 输出中稳健提取 JSON 对象（含 summary 与 items）"""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM 输出未找到 JSON 对象: {text[:200]}")
    return json.loads(text[start:end + 1])


def clean_for_js(value):
    """JS 字符串安全：内部英文双引号一律转中文引号，去首尾空白"""
    if not isinstance(value, str):
        value = str(value)
    return value.replace('"', "“").strip()


# 分类纠偏关键词
POLICY_HINTS = ["政策", "监管", "法规", "法案", "立法", "政府", "部委", "标准",
                "合规", "备案", "条例", "管理办法", "管理局", "指导意见", "规划",
                "扶持", "补贴", "政府采购", "市级", "省级"]
CAPITAL_HINTS = ["融资", "并购", "收购", "ipo", "上市", "估值", "投资", "注资",
                 "增资", "参股", "领投", "跟投", "募资", "轮"]
NEGATIVE_HINTS = ["黑客", "攻击", "入侵", "罚款", "起诉", "诉讼", "争议", "泄露",
                  "宕机", "故障", "被罚", "违规", "反垄断", "泄密"]


def normalize_category(cat, title, desc):
    """分类确定性纠偏。

    模型常为了让各分类数量好看而错标（例如把"某公司陷入安全争议"标成政策发布）。
    这里按关键词兜底校正：政策发布必须有政策类词汇，投融资必须有资本类词汇，
    技术突破不得用于负面事件。
    """
    text = (str(title or "") + str(desc or "")).lower()
    if cat == "policy" and not any(k in text for k in POLICY_HINTS):
        return "industry"
    if cat == "capital" and not any(k in text for k in CAPITAL_HINTS):
        return "industry"
    if cat == "tech" and any(k in text for k in NEGATIVE_HINTS):
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


def generate_news(material, today_cn, attempt=0):
    prompt = build_prompt(material, today_cn, attempt=attempt)
    # 重试时把 temperature 从 0.4 提到 0.6，扩大选题多样性
    raw = call_glm(prompt, temperature=0.4 if attempt == 0 else 0.6)
    obj = parse_llm_json(raw)
    raw_items = obj.get("items", []) if isinstance(obj, dict) else []

    valid_urls = {m["url"] for m in material}
    material_text = " ".join(m.get("title", "") for m in material)
    out = []
    for it in raw_items:
        cat = str(it.get("cat", "")).strip().lower()
        if cat not in CAT_LABELS:
            continue
        url = str(it.get("url", "")).strip()
        if url not in valid_urls:      # URL 白名单强校验
            log(f"  丢弃编造 URL 的条目: {it.get('title', '')[:30]} url={url}")
            continue
        title = clean_for_js(it.get("title", ""))[:60]
        desc = clean_for_js(it.get("desc", ""))[:400]
        src = clean_for_js(it.get("source", ""))[:30]
        if not title or not desc or not url:
            continue
        # 数字溯源：金额/估值/百分比/倍数必须能在素材里找到，否则丢弃该条
        if not numbers_grounded(title, material_text) or not numbers_grounded(desc, material_text):
            log(f"  丢弃数字不可核实的条目: {title[:30]}")
            continue
        # 聚合类 / 消费电子类标题
        if title_blocked(title):
            log(f"  丢弃聚合或消费电子类条目: {title[:30]}")
            continue
        # desc 残留英文句子 → 翻译未完成
        if stray_english_count(desc) >= 3:
            log(f"  丢弃英文残留的条目: {title[:30]}")
            continue
        # 分类确定性纠偏（模型常为"四类均衡"而错标）
        fixed = normalize_category(cat, title, desc)
        if fixed != cat:
            log(f"  分类纠偏: {cat}→{fixed}  {title[:24]}")
            cat = fixed
        out.append({
            "cat": cat,
            "catLabel": CAT_LABELS[cat],
            "title": title,
            "desc": desc,
            "source": src,
            "url": url,
        })
    summary = clean_for_js(obj.get("summary", ""))[:120] if isinstance(obj, dict) else ""
    if out and not summary_consistent(summary, out):
        log("  摘要提及了未收录内容，改用条目标题兜底摘要")
        summary = clean_for_js(fallback_summary(out))
    return summary, out


# ---------------------------------------------------------------------------
# HTML 写入
# ---------------------------------------------------------------------------
def js_literal(items, date_str, weekday, summary):
    """构造一段可插入 NEWS_DATA 的 JS 对象文本"""
    item_lines = []
    for i in items:
        item_lines.append(
            f'      {{ cat: "{i["cat"]}", catLabel: "{i["catLabel"]}", '
            f'title: "{i["title"]}", desc: "{i["desc"]}", '
            f'source: "{i["source"]}", url: "{i["url"]}" }}'
        )
    block = (
        "  {\n"
        f'    date: "{date_str}",\n'
        f'    weekday: "{weekday}",\n'
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

    # 1. 抓素材
    material = collect_material(hours=48)
    if len(material) < 8:
        log("素材不足（<8 条），本次跳过，避免生成低质/编造内容")
        return 0

    # 2. LLM 生成：默认目标 8 条，单次不足自动重试一次（提高 temperature 扩选题）
    summary, items = "", []
    last_err = None
    for attempt in range(2):
        try:
            summary, items = generate_news(material, today_cn, attempt=attempt)
            log(f"第{attempt+1}次生成 {len(items)} 条（target=8）")
        except Exception as e:
            last_err = e
            log(f"第{attempt+1}次 LLM 生成失败: {e}")
            continue
        if len(items) >= 8:
            break

    if not items:
        log(f"两轮生成均失败（最后错误: {last_err}），放弃本次写入")
        return 1
    if len(items) < 6:
        log(f"两次重试仍只有 {len(items)} 条（<6 硬底线），放弃本次写入")
        return 1
    if len(items) < 8:
        log(f"仅生成 {len(items)} 条（<8 条），仍写入但内容偏少")
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
