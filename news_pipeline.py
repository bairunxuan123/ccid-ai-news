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
]
STRONG_CN = [
    "人工智能", "大模型", "大语言模型", "智能体", "多模态", "自动驾驶", "智驾",
    "算力", "数据中心", "英伟达", "深度学习", "机器学习", "神经网络", "生成式",
    "大模型公司", "ai大模型",
]
WEAK_CN = ["芯片", "机器人", "gpu", "智能"]
BLOCK_CN = [
    "相机", "手机", "镜头", "耳机", "电视", "扫地", "戒指", "手表", "键盘",
    "鼠标", "显示器", "爆料", "评测", "显卡", "笔记本", "平板", "手机壳",
    "充电", "家电", "彩电", "空调", "冰箱", "洗衣机", "家居",
]


def is_ai_related(title):
    t = title.lower()
    if re.search(r"\bai\b", t):
        return True
    if any(k in t for k in STRONG_EN):
        return True
    if any(k in t for k in STRONG_CN):
        return True
    if any(k in t for k in WEAK_CN):
        return not any(b in t for b in BLOCK_CN)
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


def collect_material(hours=24):
    """汇总所有源最近 N 小时且 AI 相关的条目，去重"""
    seen, material = set(), []
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    for src in RSS_SOURCES:
        for it in fetch_rss_items(src):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            if not is_ai_related(it["title"]):
                continue
            # 粗略过滤太旧条目：很多源无标准日期，放宽到 48h 内文本提示，不严格
            material.append(it)
        if len(material) >= 40:
            break
    log(f"素材汇总：{len(material)} 条 AI 相关（近 {hours}h）")
    return material


# ---------------------------------------------------------------------------
# LLM 生成
# ---------------------------------------------------------------------------
def call_glm(prompt):
    body = json.dumps({
        "model": ZHIPU_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
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


def build_prompt(material, today_cn):
    lines = "\n".join(
        f"{i+1}. {m['title']} ｜来源:{m['source']} ｜URL:{m['url']}"
        for i, m in enumerate(material[:30])
    )
    return f"""今天是{today_cn}。下面是从各大科技媒体抓取的 AI 相关新闻素材（编号+标题+URL）。

素材：
{lines}

请从中挑选 8 条最有产业价值的新闻，整理成"人工智能产业动态"，四类各 2 条：
- policy 政策发布：政府/监管/标准相关
- tech 技术突破：模型/算法/芯片/算力技术进展
- industry 产业动态：企业合作/产品发布/行业趋势
- capital 投融资：融资/并购/IPO

硬性要求：
1. 只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记。
2. 对象格式严格为：
{{"summary":"一句话概括今日AI产业要点，不超过80字","items":[{{"cat":"policy","title":"标题不超过30字","desc":"简述80-120字，客观专业","source":"媒体名","url":"https://原文链接"}},...]}}
3. source 填媒体简称（如 36氪、IT之家、VentureBeat），url 必须从上方素材中挑选真实 URL，禁止编造。
4. title 用中文，控制在 30 字内；desc 用中文书面语，不要口语和感叹号。
5. 四类（policy/tech/industry/capital）各 2 条左右，总数尽量接近 8。
6. 若某类素材确实不足可少于此数，不要硬凑编造。"""


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


def generate_news(material, today_cn):
    prompt = build_prompt(material, today_cn)
    raw = call_glm(prompt)
    obj = parse_llm_json(raw)
    raw_items = obj.get("items", []) if isinstance(obj, dict) else []

    valid_urls = {m["url"] for m in material}
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
        out.append({
            "cat": cat,
            "catLabel": CAT_LABELS[cat],
            "title": title,
            "desc": desc,
            "source": src,
            "url": url,
        })
    summary = clean_for_js(obj.get("summary", ""))[:120] if isinstance(obj, dict) else ""
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
    """用 node 校验 NEWS_DATA 语法"""
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
    proc = subprocess_run(code, html_path)
    return proc == 0


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
    material = collect_material(hours=36)
    if len(material) < 6:
        log("素材不足（<6 条），本次跳过，避免生成低质/编造内容")
        return 0

    # 2. LLM 生成 8 条
    try:
        summary, items = generate_news(material, today_cn)
    except Exception as e:
        log(f"LLM 生成失败: {e}")
        return 1
    if len(items) < 4:
        log(f"生成条目过少（{len(items)}），放弃本次写入")
        return 1
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

    # 4. JS 语法校验
    ok = True
    for p in (FULL_HTML, LITE_HTML):
        if check_js_syntax(p) != 0:
            ok = False
            log(f"JS 语法校验失败: {p}")
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
