#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补齐缺失日期的 AI 产业动态（一次性回溯脚本）

与 news_pipeline.py 同源逻辑：
  - 复用 RSS 抓取 + AI 相关性过滤
  - 按 RSS 发布日把素材分组，对每个缺失日期单独调 GLM 生成 8 条
  - URL 强制白名单（只接受素材里真实存在的链接）
  - 按日期倒序插入两个 HTML 的 NEWS_DATA 头部，并做 node JS 语法校验

用法：
  ZHIPU_API_KEY=xxx python3 backfill_news.py 2026-09-09 2026-09-14
"""
import json
import os
import re
import sys
import time
import email.utils
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_pipeline as np

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
FULL_HTML = os.path.join(WORKSPACE, "ai-chain-map.html")
LITE_HTML = os.path.join(WORKSPACE, "ai-chain-map-lite.html")


def parse_day(published):
    """从 RSS 日期字符串取出 YYYY-MM-DD，失败返回 None"""
    if not published:
        return None
    try:
        return email.utils.parsedate_to_datetime(published).strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(published.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return None


def collect_all():
    """抓全部源，返回 [{title,url,source,published,day}]（不去重之外的过滤）"""
    seen, out = set(), []
    for src in np.RSS_SOURCES:
        items = np.fetch_rss_items(src)
        np.log(f"  {src} → {len(items)} 条")
        for it in items:
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            it["day"] = parse_day(it["published"])
            out.append(it)
    np.log(f"总去重素材：{len(out)} 条")
    return out


def day_material(all_items, day):
    """取某天的 AI 相关素材；不足 6 条时放宽到"AI 邻域"（仍有明确口径，不放行消费电子噪声）"""
    same_day = [it for it in all_items if it["day"] == day]
    strict = [it for it in same_day if np.is_ai_related(it["title"])]
    if len(strict) >= 6:
        return strict, len(same_day)
    adjacent = [it for it in same_day if np.is_ai_adjacent(it["title"])]
    # 邻域集合至少不劣于 strict
    merged = strict + [it for it in adjacent if it not in strict]
    return merged, len(same_day)


def build_day_prompt(material, day_str):
    weekday = np.WEEKDAYS[datetime.strptime(day_str, "%Y-%m-%d").weekday()]
    lines = "\n".join(
        f"{i+1}. {m['title']} ｜来源:{m['source']} ｜URL:{m['url']}"
        for i, m in enumerate(material[:30])
    )
    return f"""下面是{day_str}（{weekday}）当天各大科技媒体发布的 AI 相关新闻素材（编号+标题+URL）。

素材：
{lines}

请从中挑选 4-8 条当日最有产业价值的 AI 新闻，整理成"人工智能产业动态"。**质量优先于数量：当日 AI 素材少就少写几条（4 条即可），绝不要为凑数收录无关新闻。** 若当日确实素材稀少（例如周末），可以少于 4 条，最少 2 条；但任何情况下都不得为了凑数收录与 AI 产业无关的内容。

分类口径（必须严格按新闻实质判断，宁缺勿错）：
- policy 政策发布：政府部门、监管机构、行业标准、法律法规相关
- tech 技术突破：模型/算法/芯片/算力/产品技术本身的进展
- industry 产业动态：企业合作、产品上市、产能布局、行业趋势、企业业绩
- capital 投融资：融资、并购、IPO、估值变化

硬性要求：
1. 只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记。
2. 对象格式严格为：
{{"summary":"一句话概括当日AI产业要点，不超过80字","items":[{{"cat":"policy","title":"标题不超过30字","desc":"简述80-120字，客观专业","source":"媒体名","url":"https://原文链接"}},...]}}
3. source 填媒体简称（如 IT之家、TechCrunch、The Verge），url 必须从上方素材中挑选真实 URL，禁止编造、拼接或改写。
4. title 用中文，控制在 30 字内，须是新闻事实的准确概括，不要加评价性形容词；desc 用中文书面语客观陈述，不要口语和感叹号。
5. **不要为了凑齐"每类 2 条"而错标分类**。若某一类当日确实没有对应新闻，该类可以为 0 条，四类数量允许不均衡（例如 3/3/2/0）。错标分类比数量不均衡严重得多。
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


def gen_day(material, day_str):
    """某天生成，最多重试 3 次；后两次逐步收紧（禁数字 → 只取最稳妥的条目）"""
    valid_urls = {m["url"] for m in material}
    material_text = " ".join(m.get("title", "") for m in material)
    for attempt in range(3):
        try:
            prompt = build_day_prompt(material, day_str)
            if attempt == 2:
                # 末次尝试：允许放宽数字口径（中英文金额换算已归一化，此处仅提示更保守）
                prompt += (
                    "\n\n【本次为最后一次尝试，请务必满足条数要求】"
                    "请优先挑选不涉及金额、估值、百分比的新闻，"
                    "若确实需要提及金额请使用素材中的原始写法（如 $500M 写作 5亿美元）。"
                )
            raw = np.call_glm(prompt)
            obj = np.parse_llm_json(raw)
        except Exception as e:
            np.log(f"  {day_str} 第{attempt+1}次生成失败: {e}")
            time.sleep(3)
            continue
        items = []
        for it in (obj.get("items") or []):
            cat = str(it.get("cat", "")).strip().lower()
            url = str(it.get("url", "")).strip()
            if cat not in np.CAT_LABELS:
                continue
            if url not in valid_urls:
                np.log(f"  丢弃编造 URL: {str(it.get('title',''))[:26]}")
                continue
            title = np.clean_for_js(it.get("title", ""))[:60]
            desc = np.clean_for_js(it.get("desc", ""))[:400]
            src = np.clean_for_js(it.get("source", ""))[:30]
            if not (title and desc and src):
                continue
            if not np.numbers_grounded(title, material_text) or not np.numbers_grounded(desc, material_text):
                np.log(f"  丢弃数字不可核实的条目: {title[:26]}")
                continue
            if np.title_blocked(title):
                np.log(f"  丢弃聚合或消费电子类条目: {title[:26]}")
                continue
            if np.stray_english_count(desc) >= 3:
                np.log(f"  丢弃英文残留的条目: {title[:26]}")
                continue
            # 分类确定性纠偏
            fixed = np.normalize_category(cat, title, desc)
            if fixed != cat:
                np.log(f"  分类纠偏: {cat}→{fixed}  {title[:24]}")
                cat = fixed
            items.append({
                "cat": cat, "catLabel": np.CAT_LABELS[cat],
                "title": title, "desc": desc, "source": src, "url": url,
            })
        if len(items) >= 2:
            summary = np.clean_for_js(obj.get("summary", ""))[:120]
            if not np.summary_consistent(summary, items):
                np.log("  摘要提及了未收录内容，改用条目标题兜底摘要")
                summary = np.clean_for_js(np.fallback_summary(items))
            return summary, items
        np.log(f"  {day_str} 第{attempt+1}次仅 {len(items)} 条，重试")
        time.sleep(2)
    return "", []


def js_block(day_str, weekday, summary, items):
    """按现有 HTML 的缩进风格构造条目块"""
    per = [",\n".join(
        "      {\n"
        f'        cat: "{i["cat"]}",\n'
        f'        catLabel: "{i["catLabel"]}",\n'
        f'        title: "{i["title"]}",\n'
        f'        desc: "{i["desc"]}",\n'
        f'        source: "{i["source"]}",\n'
        f'        url: "{i["url"]}"\n'
        "      }" for i in items)]
    return (
        "  {\n"
        f'    date: "{day_str}",\n'
        f'    weekday: "{weekday}",\n'
        f'    summary: "{summary}",\n'
        "    items: [\n"
        + ",\n".join(per) + "\n"
        "    ]\n"
        "  },\n"
    )


# 匹配每个日期块的起始（"  {" + 下一行的 date），数组第一个块同样能命中
BLOCK_START_RE = re.compile(r'  \{\n    date: "(\d{4}-\d{2}-\d{2})"')
# 日期块的结束标记（items 数组收尾 + 对象收尾）
BLOCK_END = "\n    ]\n  },\n"
# NEWS_DATA 数组的结束位置
ARRAY_END_RE = re.compile(r"\n\];")


def find_block(content, day, start=0):
    """定位某日期块在 content 中的 [start, end) 偏移；找不到返回 None"""
    m = BLOCK_START_RE.search(content, start)
    while m:
        if m.group(1) == day:
            e = content.find(BLOCK_END, m.end())
            if e == -1:
                return None
            return m.start(), e + len(BLOCK_END)
        m = BLOCK_START_RE.search(content, m.end())
    return None


def insert_blocks(html_path, blocks, days, replace=False):
    """把若干天条目按日期倒序插入 NEWS_DATA 的正确位置。

    不能一律插到数组最前：当回溯日期中存在缺口（例如 9-11 无内容）时，
    盲目前插会破坏"新→旧"顺序。这里按日期找到第一个更早的块，插到它前面；
    若目标日期比现有全部更早，则追加到数组末尾（并补上必要的逗号）。

    replace=True 时先删除这些日期的旧块再插入，用于质量不合格时的重生成，
    这样无需把线上页面回滚到旧版本。
    """
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    anchor = "var NEWS_DATA = [\n"
    idx = content.find(anchor)
    if idx == -1:
        raise RuntimeError(f"{html_path} 未找到 NEWS_DATA 锚点")
    head = idx + len(anchor)

    if replace:
        # 从后往前删，避免先删导致后续偏移失效
        spans = [sp for d in set(days) if (sp := find_block(content, d, head))]
        for s, e in sorted(spans, reverse=True):
            content = content[:s] + content[e:]
        if spans:
            np.log(f"  已移除 {len(spans)} 个旧块，准备重生成")

    written = 0
    for day, blk in zip(days, blocks):
        if f'date: "{day}"' in content:
            np.log(f"  {day} 已存在，跳过")
            continue
        # 现有块位置（按文中顺序＝日期倒序）
        entries = [(m.start(), m.group(1)) for m in BLOCK_START_RE.finditer(content, head)]
        pos = None
        for off, d in entries:
            if d < day:          # 第一个比目标日期更早的块 → 插到它前面
                pos = off
                break
        if pos is None:
            # 目标日期比现有全部更早：追加到数组末尾（末块需补逗号）
            me = ARRAY_END_RE.search(content, head)
            if me is None:
                raise RuntimeError(f"{html_path} 未找到 NEWS_DATA 结束标记")
            ins = "," + blk.rstrip("\n").rstrip(",") + "\n"
            content = content[:me.start()] + ins + content[me.start():]
        else:
            content = content[:pos] + blk + content[pos:]
        written += 1
    if written == 0 and not replace:
        return False
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(content)
    np.log(f"  已写入 {html_path}（{written} 天）")
    return written > 0


def main():
    args = sys.argv[1:]
    replace = "--replace" in args
    days = [a for a in args if not a.startswith("--")]
    if not days:
        print("用法: backfill_news.py [--replace] 2026-09-09 2026-09-14 [...]")
        return 2
    if not np.ZHIPU_API_KEY:
        np.log("缺少 ZHIPU_API_KEY")
        return 2

    np.log("抓取全部素材 ...")
    all_items = collect_all()

    results = []
    for day in days:
        material, raw_n = day_material(all_items, day)
        np.log(f"{day}: 当日原始 {raw_n} 条，可用素材 {len(material)} 条")
        if len(material) < 2:
            np.log(f"  素材不足，跳过 {day}")
            continue
        summary, items = gen_day(material, day)
        if len(items) < 2:
            np.log(f"  生成失败，跳过 {day}")
            continue
        weekday = np.WEEKDAYS[datetime.strptime(day, "%Y-%m-%d").weekday()]
        results.append((day, js_block(day, weekday, summary, items), len(items)))
        np.log(f"  ✓ {day} 生成 {len(items)} 条 | {summary[:40]}")
        time.sleep(1)

    if not results:
        np.log("无任何可用结果")
        return 1

    # 按日期倒序排列（最新在前）
    results.sort(key=lambda x: x[0], reverse=True)
    days_sorted = [r[0] for r in results]
    blocks = [r[1] for r in results]
    for p in (FULL_HTML, LITE_HTML):
        insert_blocks(p, blocks, days_sorted, replace=replace)

    # JS 语法校验
    bad = False
    for p in (FULL_HTML, LITE_HTML):
        if np.check_js_syntax(p) is False:
            bad = True
            np.log(f"JS 校验失败: {p}")
    if bad:
        return 1
    np.log(f"全部完成：写入 {len(results)} 天，日期 {days_sorted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
