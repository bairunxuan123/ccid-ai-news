#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[2026-10-19] 三道新护栏，修复当天暴露的两类漏网：
1) "工信部人工智能+软件方案"：9-2 旧闻、标题与正文错配（正文是统计局数据）。
   → 新增 title_desc_coherent()：标题里的机构名必须在正文出现，
     拦截"标题 A 素材 + 正文 B 素材"的错配条目。
2) "孝老爱老购物季"：真新闻但与 AI 无关（仅蹭"智能护理机器人"）。
   → 政策类 AI 主题由宽表（机器人/数据/算法都算）收紧为强主题词表
     （人工智能/大模型/算力/智能体/生成式…），国内外政策口径同时收紧。

锚点校验式：任一锚点不命中即中止，绝不盲改。
"""
import re
import sys

PIPE = "news_pipeline.py"
BACK = "backfill_news.py"


def patch(path, edits):
    src = open(path, encoding="utf-8").read()
    for old, new, tag in edits:
        if new in src and tag != "must-absent":
            print(f"  [跳过] {path}: {tag} 已存在")
            continue
        if tag == "must-absent":
            if old not in src:
                print(f"  [跳过] {path}: {tag} 不存在（可能已改）")
                continue
            assert src.count(old) == 1, f"锚点不唯一: {tag}"
            src = src.replace(old, new)
            print(f"  [OK] {path}: {tag}")
            continue
        n = src.count(old)
        assert n >= 1, f"锚点未命中: {tag}"
        assert n == 1, f"锚点不唯一({n}): {tag}"
        src = src.replace(old, new)
        print(f"  [OK] {path}: {tag}")
    open(path, "w", encoding="utf-8").write(src)


GUARD_BLOCK = '''

# ---------------------------------------------------------------------------
# [2026-10-19] 政策类 AI 强主题词表。
# 原先政策类沿用 has_ai_topic()（机器人/数据/算法都算 AI 相关），
# 实测后果：三部门"孝老爱老购物季"通知（仅蹭一句"智能护理机器人"）
# 被判成 AI 政策上页面。政策发布必须与 AI 有实质关联，
# 只认强主题词；"机器人""数据""算法"这类宽泛词不再单独撑起政策条目。
POLICY_AI_STRONG = [
    "人工智能", "大模型", "算力", "智能体", "生成式", "AGI",
    "软件业", "软件产业", "软件和信息", "数据要素", "数字经济",
]


def policy_ai_strong(text):
    """政策类目专用的 AI 强主题判定（比 has_ai_topic 严格）。"""
    t = (text or "").lower()
    if re.search(r"(?<![a-z])ai(?![a-z])", t):
        return True
    return any(k.lower() in t for k in POLICY_AI_STRONG)


_ORG_SUFFIX_RE = re.compile(
    r"[\\u4e00-\\u9fff]{2,12}(?:委员会|发改委|发展改革委|工业和信息化部|部|局|"
    r"研究院|研究所|实验室|公司|集团|大学|银行|理事会|办公室)")


def title_desc_coherent(title, desc):
    """标题里的机构名必须在正文出现（至少一个），拦截标题与正文错配的条目。

    实测依据（2026-10-19）：阶段 A 给出的标题是"工信部印发《人工智能+软件
    专项行动实施方案》"（9-2 旧闻、素材池里根本没有这条），阶段 B 却按
    统计局 GDP 素材写了正文——正文通篇没有一个机构名。标题与正文
    各说各话，说明选题与写稿用的不是同一事实，直接丢弃。
    """
    orgs = {m.group(0) for m in _ORG_SUFFIX_RE.finditer(title or "")}
    if not orgs:
        return True
    d = desc or ""
    return any(o in d for o in orgs)
'''

patch(PIPE, [
    # 1) 在 is_cn_official_policy 前插入护栏块
    ('def is_cn_official_policy(title, desc=""):',
     GUARD_BLOCK + '\ndef is_cn_official_policy(title, desc=""):',
     "guard-block"),
    # 2) 国内政策：宽 AI 主题 → 强主题
    ('''    if not any(k in text for k in POLICY_ACTIONS):
        return False
    return has_ai_topic(t)''',
     '''    if not any(k in text for k in POLICY_ACTIONS):
        return False
    # [2026-10-19] 政策必须与 AI 有实质关联（强主题词），防"购物季通知"
    # 这类仅蹭"智能护理机器人"的泛化条目混入
    return policy_ai_strong(t)''',
     "cn-policy-strong-topic"),
    # 3) 国外政策同样收紧
    ('''            r"(?<![a-z])(?:rule|rules|regulation|regulations|act|ban|order|"
            r"mandate|directive|guideline|framework|bill)(?![a-z])", text.lower()):
        return False
    return has_ai_topic(text)''',
     '''            r"(?<![a-z])(?:rule|rules|regulation|regulations|act|ban|order|"
            r"mandate|directive|guideline|framework|bill)(?![a-z])", text.lower()):
        return False
    return policy_ai_strong(text)''',
     "intl-policy-strong-topic"),
    # 4) 主管线组装处：标题-正文一致性终检
    ('''        # 分类确定性纠偏（模型常为"四类均衡"而错标）。
        # [2026-09-21] 必须带上地域——"政策发布"在两个板块的判据不同：
        # 国内只认中国官方发文，国外只认境外政府/监管机构的正式规则。
        fixed = normalize_category(c["cat"], c["title"], desc, c.get("region", "cn"))''',
     '''        # [2026-10-19] 标题与正文必须说同一件事（标题机构名须见于正文）
        if not title_desc_coherent(c["title"], desc):
            log(f"  丢弃标题与正文错配的条目: {c['title'][:26]}")
            continue
        # 分类确定性纠偏（模型常为"四类均衡"而错标）。
        # [2026-09-21] 必须带上地域——"政策发布"在两个板块的判据不同：
        # 国内只认中国官方发文，国外只认境外政府/监管机构的正式规则。
        fixed = normalize_category(c["cat"], c["title"], desc, c.get("region", "cn"))''',
     "pipeline-coherent-check"),
])

patch(BACK, [
    # 回填脚本组装处：同一道终检
    ('''            # 分类确定性纠偏必须带地域：两个板块的"政策发布"判据不同
            fixed = np.normalize_category(c["cat"], c["title"], desc, c.get("region", "cn"))''',
     '''            # [2026-10-19] 标题与正文必须说同一件事（标题机构名须见于正文）
            if not np.title_desc_coherent(c["title"], desc):
                np.log(f"  丢弃标题与正文错配的条目: {c['title'][:26]}")
                continue
            # 分类确定性纠偏必须带地域：两个板块的"政策发布"判据不同
            fixed = np.normalize_category(c["cat"], c["title"], desc, c.get("region", "cn"))''',
     "backfill-coherent-check"),
])

print("补丁完成")
