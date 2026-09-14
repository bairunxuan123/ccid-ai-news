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
    """取某天及其前后 ±1 天内的所有素材，让 LLM 自己在更宽池子里挑 AI 相关。

    真实情况：很多 RSS 源的 published 日期不准（例如 IT之家常把今天发生的标昨天），
    严格按当天分会导致素材严重不足（9-14 当日 AI 强相关只有 4 条），LLM 无法凑够 8 条。
    这里放宽到 ±1 天邻域的全部素材，让 LLM 自己判断筛选；硬过滤交给 hard_blocked/title_blocked。
    """
    from datetime import datetime
    target = datetime.strptime(day, "%Y-%m-%d")
    near = [it for it in all_items if it.get("day") and
            abs((datetime.strptime(it["day"], "%Y-%m-%d") - target).days) <= 1]
    same_day = [it for it in near if it["day"] == day]
    # AI 相关性预过滤 + 消费电子剔除：与日常流水线口径一致
    near = [it for it in near
            if np.is_ai_related(it["title"]) and not np.title_blocked(it["title"])]
    # 按源轮转交错：build_day_prompt 只截取前 N 条，不重排则列表前部会被 IT之家 占满
    buckets = {}
    for m in near:
        buckets.setdefault(m["source"], []).append(m)
    inter, i = [], 0
    while any(len(v) > i for v in buckets.values()):
        for v in buckets.values():
            if len(v) > i:
                inter.append(v[i])
        i += 1
    np.log(f"  {day}: 当日 {len(same_day)} 条 → ±1 邻域 AI 相关 {len(inter)} 条（已按源轮转）")
    return inter, len(same_day)


def build_day_prompt(material, day_str, attempt=0):
    weekday = np.WEEKDAYS[datetime.strptime(day_str, "%Y-%m-%d").weekday()]

    def fmt(i, m):
        sm = (m.get("summary") or "").strip()[:np.SUMMARY_IN_PROMPT]
        head = f"{i+1}. {m['title']}\n   来源：{m['source']} ｜ URL：{m['url']}"
        return f"{head}\n   摘要：{sm}" if sm else f"{head}\n   摘要：（无，仅可依据标题撰写）"

    lines = "\n".join(
        fmt(i, m) for i, m in enumerate(material[:np.PROMPT_MATERIAL_CAP])
    )
    if attempt == 0:
        temp_note = ""
    elif attempt == 1:
        temp_note = ("\n\n【提示】请仔细检查素材池，从更广的范围挑选候选 AI 产业新闻，包括但不限于："
                     "芯片厂商（英伟达/AMD/华为海思/联发科）、云厂商（阿里云/腾讯云/华为云/AWS/Azure）、"
                     "机器人厂商、模型厂商、算力/数据中心、AI 应用、AI 监管政策、AI 投融资事件。"
                     "即使素材标题看起来边缘，只要实际反映 AI 产业变化都可选用。\n\n"
                     "【上一轮被丢弃的主因是标题不达标或素材摘要太空】标题须 20-32 字"
                     "（低于 15 字、高于 40 字一律作废）。请优先挑选摘要中含具体数字与事实的新闻"
                     "——正文由后续步骤逐条撰写，需要可核验的细节支撑。")
    else:
        temp_note = ("\n\n【末次硬性要求】必须输出 " + str(np.CANDIDATE_ITEMS) + " 条候选。"
                     "素材池已包含 ±1 天共 " + str(len(material)) + " 条，请务必从 AI 相关"
                     "（含 AI 邻域）中挑出 " + str(np.CANDIDATE_ITEMS) + " 条候选，四类每类至少 3 条，"
                     "尤其别把“政策发布”和“投融资”漏掉。\n"
                     "**同时务必保证标题质量：20-32 字、三要素齐全（主体+动作+结果）、必须译成中文"
                     "（禁止把英文原标题直接当标题）**，宁可少出几条候选，也不要出丢主体的空泛标题。"
                     "若确实凑不齐，可优先扩大到 AI 邻域（芯片/算力/数据中心/机器人/智能驾驶/"
                     "语音识别/视觉识别/数字人等）补足；**即使候选数不足，也绝不允许用空泛标题充数"
                     "——质量优先于条数**，但绝对不得收录消费电子（手机/相机/耳机/显示器/家电）、"
                     "汽车新品（新车/试驾/MPV/SUV）、操作系统更新（Windows/iOS/安卓/鸿蒙）、政治人物。"
                     "系统会硬过滤。")
    return f"""下面是{day_str}（{weekday}）当天及前后共 {len(material)} 条的宽口径新闻素材（编号+标题+URL）。

素材：
{lines}

请从中挑选 AI 产业相关的新闻，整理成"人工智能产业动态"。{temp_note}

**请输出 {np.CANDIDATE_ITEMS} 条候选**（比最终需要的 8 条多出不少，因为系统会做一轮硬性过滤，
剔除消费电子/编造数字/英文残留的条目，需要留有足够冗余），
**并按产业价值从高到低排序**，系统会按四类均衡选取前 8 条：

{np.CANDIDATE_ITEMS} 条候选必须覆盖 4 类，**每类至少 3 条候选**（这样即使过滤掉几条，
最终仍能凑齐"四类各 2 条"）：
- policy 政策发布：政府部门、监管机构、行业标准、法律法规相关
- tech 技术突破：模型/算法/芯片/算力/产品技术本身的进展
- industry 产业动态：企业合作、产品上市、产能布局、行业趋势、企业业绩
- capital 投融资：融资、并购、IPO、估值变化

**重要：素材含 ±1 天邻域和非 AI 内容**。优先用当天素材；当天不足时可选用邻日（昨天/今天）的重大新闻，但 desc 中要按事件实际日期表述（"昨日/今日..."）。

分类规则细节：
- 不要为了凑数收录与 AI 产业无关的内容（消费电子/汽车新品/系统更新/政治人物等）——系统会硬过滤拦截。
- **四类缺一不可**：实测最容易漏的是 industry 产业动态（企业合作、新品上市、产能订单、行业数据这类
  新闻），请专门去素材里找几条。若某类当日素材确实太少，也至少要给 2 条候选；
  绝对不要整类不写——系统按类均衡选取，缺了哪一类最终成品就会缺哪一类。

分类口径（必须严格按新闻实质判断，宁缺勿错）：
- policy 政策发布：政府部门、监管机构、行业标准、法律法规相关
- tech 技术突破：模型/算法/芯片/算力/产品技术本身的进展
- industry 产业动态：企业合作、产品上市、产能布局、行业趋势、企业业绩
- capital 投融资：融资、并购、IPO、估值变化

============== 写作标准（本项目定版风格，务必逐条对齐）==============

【标题：20-32 字，三要素齐全】
1. **主体 + 动作 + 结果**齐全。主体必须是具体机构名或公司名（如"国家发改委""交通运输部""Stripe""宇树科技"），禁止"某公司""相关部门"这类模糊主体。
2. **尽量带数字**：金额、数量、规模、时间、比例。参考基准：定版风格中 56% 的标题含数字。
3. **约三分之一的标题使用双分句**（逗号连接）：前半句陈述事实，后半句点出结果或意义。
4. 可用冒号引出细节（如"国家发改委：加快人工智能法立法进程"）。
5. 严禁丢掉主体或事件信息的空泛写法。

✅ 标题范例（照此风格撰写）：
- 两部门联合发布AI计量体系指引，破解测不准与数据荒
- 北京亦庄建成首个词元工厂，日产1.4万亿词元
- Stripe 75亿美元收购OpenRouter
- 宇树科技科创板挂牌，人形机器人第一股诞生
❌ 禁用写法："AI政策窗口开放"、"Nvidia解释增长原因"、"某公司面临挑战"（均丢了主体或事件信息）。

【正文：本轮不用写，但选题要为其负责】
正文 desc 由后续步骤单独逐条撰写（本步只出选题与标题）。
因此选题时请**优先挑选"摘要里含具体事实与数字"的新闻**（金额、数量、时间、占比、
技术规格、合作方数量）——正文必须有可核验的细节支撑，摘要空洞的素材写不出合规正文。
✅ 参考：定版正文平均 119 字，写法是"谁做了什么 → 关键细节（含数字）→ 影响或后续计划"。
❌ 禁用：仅十几个字的短语（实测出现过 11 字正文），系统会直接丢弃。

硬性要求：
1. 只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块标记。
2. 对象格式严格为：
{{"summary":"一句话概括当日AI产业要点，不超过80字","items":[{{"cat":"policy","title":"标题20-32字","source":"媒体名","url":"https://原文链接"}},...]}}
   **注意：items 里不要写 desc 字段**，正文留待后续步骤生成。
3. source 填媒体简称（如 IT之家、TechCrunch、The Verge），url 必须从上方素材中挑选真实 URL，禁止编造、拼接或改写。**同一条素材只能出现一次**。
4. title 用中文，控制在 20-30 字，须是新闻事实的准确概括，不要加评价性形容词。
5. **不要为了凑齐"每类 2 条"而错标分类**。若某一类当日实在没有对应新闻（极少），该类可以为 1 条，但其它类补足 8 条总数；不要硬塞错标条目充数。错标分类比数量不均衡严重得多。
6. 输出前逐条自查：这条新闻的实质与所标分类是否一致？不一致就改正分类或换掉该条。
7. **绝对排除**与 AI 产业无关的内容：消费电子新品（手机/相机/耳机/显示器/笔记本）、汽车新车与试驾（含 MPV/SUV 官图）、灯光与外设软件、操作系统更新（Windows/iOS/安卓的系统或功能更新）、产品与发布会预告、游戏影视娱乐、体育赛事、社会新闻——素材里出现也不要选。
8. 选题限于产业与技术范畴：判断标准是"这条新闻是否直接反映 AI 产业或技术本身的变化"。凡属个人公开表态、社会活动、与产业无关的公共事务，一律不选。
9. **数字必须来自素材**：素材标题与摘要中出现的金额、估值、百分比、增长倍数、技术规格可以放心使用，这正是正文该有的信息密度；**素材中没有的数字一律不得出现**（后续步骤写正文时会校验并丢弃含无法核实数字的条目）。摘要缺失时改用定性描述（如"大幅增长""估值处于高位"）。
10. **标题必须忠实于原文事实**：素材多为英文，须准确理解后再译为中文，不得截取英文原句、不得把原文没有的判断归纳进标题。例如原文讲"为 AI 供电是架构问题"，就不能写成"AI 在音频内容中的应用"。
11. **标题以中文书面语为主**，不得残留整句英文；但公司名、产品名、模型名与技术术语（OpenAI、Apache Fluss、TPU、token）保留英文原名，不要生硬音译。
12. 不要选用"早报/日报/盘点/汇总/速览"这类聚合内容，也不要选消费电子（iOS/iPhone/手机/相机/耳机）与汽车新品——素材里出现也不要选。
13. **标题不得泛化**：必须保留原文的核心主体与事件（谁做了什么）。反面示例（实测出现过，一律禁止）："AI政策窗口开放"（没说是谁提的什么政策）、"Meta调整AI建议功能"（没说调整什么、为什么）、"Nvidia解释增长原因"（没说是谁问的、解释了哪项增长）、"发布脑机接口标准"（丢了主体"我国"）。
14. **summary 只能概括本次 items 里实际收录的条目**，不得提及未收录的新闻。系统会核对，出现未收录内容视为错误。
15. 分类补充口径：企业发生安全事故、被攻击、被罚款等负面事件属于"产业动态"，不要标成"技术突破"；只有当新闻本身是技术能力/模型能力的进展时才用"技术突破"。"""


def gen_day(material, day_str):
    """某天生成，最多重试 3 次；后两次逐步收紧（禁数字 → 只取最稳妥的条目）"""
    valid_urls = {m["url"] for m in material}
    # [2026-09-14] 纳入正文摘要：否则模型按 8 月标准写出摘要里的真实数字，
    # 会被 numbers_grounded 判为"编造数字"整条丢弃（反向 bug）。
    material_text = " ".join(
        (m.get("title", "") + " " + (m.get("summary") or "")) for m in material
    )
    for attempt in range(3):
        try:
            prompt = build_day_prompt(material, day_str, attempt=attempt)
            if attempt == 2:
                # 末次尝试：允许放宽数字口径（中英文金额换算已归一化，此处仅提示更保守）
                prompt += (
                    "\n\n【本次为最后一次尝试，请务必满足条数要求】"
                    "请优先挑选不涉及金额、估值、百分比的新闻，"
                    "若确实需要提及金额请使用素材中的原始写法（如 $500M 写作 5亿美元）。"
                )
            # 重试时把 temperature 从 0.4 提到 0.6，扩大选题多样性
            raw = np.call_glm(prompt, temperature=0.4 if attempt == 0 else 0.6)
            obj = np.parse_llm_json(raw)
        except Exception as e:
            np.log(f"  {day_str} 第{attempt+1}次生成失败: {e}")
            time.sleep(3)
            continue
        # —— 阶段 A 过滤：分类 / URL 白名单 / 标题长度 / 硬拦截 ——
        cands, seen = [], set()
        for it in (obj.get("items") or []):
            cat = str(it.get("cat", "")).strip().lower()
            url = str(it.get("url", "")).strip()
            if cat not in np.CAT_LABELS:
                continue
            if url not in valid_urls:
                np.log(f"  丢弃编造 URL: {str(it.get('title',''))[:26]}")
                continue
            if url in seen:
                np.log(f"  丢弃重复素材: {str(it.get('title',''))[:26]}")
                continue
            seen.add(url)
            title = np.clean_for_js(it.get("title", ""))[:60]
            if not title:
                continue
            # 8 月定版标准：标题 20-32 字（9 月实测出现过 8 字标题），低于下限直接丢弃
            if len(title) < np.MIN_TITLE_LEN:
                np.log(f"  丢弃标题过短（{len(title)}字）: {title}")
                continue
            if len(title) > np.MAX_TITLE_LEN:
                np.log(f"  丢弃标题过长（{len(title)}字）: {title[:26]}")
                continue
            if np.title_english_residue(title):
                np.log(f"  丢弃标题未翻译: {title[:26]}")
                continue
            # [2026-09-14 同步] 标题混入提示词指令的候选必须拦下。
            # 真实复验（run 34819377914）里模型把"上一轮不合格，本次必须逐条修正"
            # 当正文抄进了成品，标题侧同样可能被污染，与日常流水线口径保持一致。
            if np.prompt_leak(title):
                np.log(f"  丢弃标题混入提示词的条目: {title[:26]}")
                continue
            # 硬拦截：消费电子/汽车新品/系统更新/政治人物（与 daily pipeline 保持一致）
            if np.hard_blocked(title):
                np.log(f"  硬拦截: {title[:26]}")
                continue
            if np.title_blocked(title):
                np.log(f"  丢弃聚合或消费电子类条目: {title[:26]}")
                continue
            cands.append({
                "cat": cat, "title": title,
                "source": np.clean_for_js(it.get("source", ""))[:30],
                "url": url,
                "_rank": len(cands),
            })

        # —— 阶段 B：逐条素材单独撰写 110-150 字正文 ——
        # [2026-09-14] 与 news_pipeline 同步：免费模型 glm-4-flash 在批量任务里
        # 会把每条正文压到 50 字上下（实测批量 3 条 → 均 63 字），
        # 拆成单条窄任务后可稳定产出 170-210 字，才能达到 8 月定版的 118.9 字。
        # 同时把"一次不成即丢弃"改为"定向重写至多 3 轮"（见 np.desc_issues）。
        np.log(f"  {day_str} 阶段A {len(cands)} 条候选，开始逐条写正文"
               f"（四类各满 {np.MIN_PER_CAT_DESC} 条才收工，上限 {np.DESC_TARGET} 条）")
        by_url = {m["url"]: m for m in material}
        # 按类轮转，保证无论写到哪里停手四类都是齐的
        cands = np.interleave_by_category(cands, prefer=np.MIN_PER_CAT_DESC)
        items = []
        for ci, c in enumerate(cands):
            if len(items) >= np.DESC_TARGET:
                np.log(f"  已达上限 {np.DESC_TARGET} 条，其余候选不再调用")
                break
            # [2026-09-14 同步] 收工条件不只是"凑够条数"，还要四类都各有
            # MIN_PER_CAT_DESC 条供 select_balanced 均衡选取 —— 否则回填出来的
            # 某天会出现四类缺项（真实复验 run 34819377914 缺的就是"产业动态"）。
            if len(items) >= np.MAX_ITEMS and np._cat_ready(items, np.MIN_PER_CAT_DESC):
                np.log(f"  已写满 {len(items)} 条且四类齐备，其余候选不再调用")
                break
            if ci:
                time.sleep(1.0)
            m = by_url[c["url"]]
            desc, issues, got_any = "", {}, False
            for b in range(3):
                try:
                    got = np.call_glm(
                        np.build_desc_prompt(m, attempt=b, fix=issues if b else None),
                        temperature=0.5 if b == 0 else 0.35)
                except Exception as e:
                    np.log(f"  正文生成失败（{c['title'][:18]}）: {e}")
                    continue
                got_any = True
                cand_desc = np.clean_for_js(np.clean_desc_output(got))[:400]
                if not cand_desc:
                    continue
                desc = cand_desc
                issues = np.desc_issues(desc, material_text)
                if not issues:
                    break
                # 用公共函数汇总原因：5 项体检（长度/数字/英文/混入指令/空泛表述）
                # 任何一项漏打印，日志就会指向"无理由地重写"
                np.log(f"  正文待修（第{b+1}轮）{np.issues_brief(issues)}: {c['title'][:20]}")
            if not got_any or issues:
                np.log(f"  丢弃重写仍不合格的条目: {c['title'][:26]}")
                continue
            if np.ungrounded_numbers(c["title"], material_text):
                np.log(f"  丢弃标题数字不可核实的条目: {c['title'][:26]}")
                continue
            # 分类确定性纠偏
            fixed = np.normalize_category(c["cat"], c["title"], desc)
            if fixed != c["cat"]:
                np.log(f"  分类纠偏: {c['cat']}→{fixed}  {c['title'][:24]}")
            items.append({
                "cat": fixed, "catLabel": np.CAT_LABELS[fixed],
                "title": c["title"], "desc": desc,
                "source": c["source"] or m["source"], "url": c["url"],
                "_rank": c.get("_rank", 999),
            })
        if len(items) >= 6:
            # 各类实际写出多少条合格正文（排查"四类缺项"的分界信息，与日常流水线一致）
            _dist = {}
            for x in items:
                _dist[x["cat"]] = _dist.get(x["cat"], 0) + 1
            np.log(f"  {day_str} 阶段B 合格正文：" + " ".join(
                f"{np.CAT_LABELS[k]}{_dist.get(k, 0)}" for k in np.CAT_LABELS))
            # 先还原阶段 A 的价值序，再去话题重复项、最后四类均衡选取（须在摘要校验前）
            items.sort(key=lambda x: x.get("_rank", 999))
            for x in items:
                x.pop("_rank", None)
            # [2026-09-14 同步] 同主体同类的重复话题只留价值序靠前的一条
            # （真实复验里 OpenAI 上市/控速两条白占了两个名额）
            items = np.dedupe_similar(items)
            items = np.select_balanced(items)
            summary = np.clean_for_js(obj.get("summary", ""))[:120]
            if not np.summary_consistent(summary, items):
                np.log("  摘要提及了未收录内容，改用条目标题兜底摘要")
                summary = np.clean_for_js(np.fallback_summary(items))
            return summary, items
        np.log(f"  {day_str} 第{attempt+1}次仅 {len(items)} 条（<6 硬底线），重试")
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
        if len(material) < 6:
            np.log(f"  素材不足 6 条（含邻域），放弃 {day}")
            continue
        summary, items = gen_day(material, day)
        if len(items) < 6:
            np.log(f"  生成失败（<6 条），跳过 {day}")
            continue
        if len(items) < 8:
            np.log(f"  ⚠ 仅 {len(items)} 条（<8 目标），仍写入")
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
