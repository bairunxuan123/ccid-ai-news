#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本：查清"模型为何写不出 100+ 字正文"。

背景：8 月及 9-08 前的内容由 WorkBuddy 智能体（强模型+联网搜索）生成，正文 80-165 字；
9-09 云端接管后改用免费模型 glm-4-flash，正文骤降到 22-36 字，
即使提示词三次明确要求"100-160 字、不足 80 字作废"仍然只写 40-70 字。

本脚本一次跑完，回答两个问题：
  Q1 是提示词太长/太复杂导致的，还是模型能力问题？  → 对比 3 种提示词变体
  Q2 glm-4-flash 是否撞了输出 token 上限？          → 打印 finish_reason 与 usage
  Q3 智谱账号能用哪些更强的模型？                    → 逐个探测可用性

输出全部写到 stdout，供云端日志查看。
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_pipeline as np  # noqa: E402

DAY = sys.argv[1] if len(sys.argv) > 1 else None


def call(model, prompt, temperature=0.4, max_tokens=4096):
    """调一次智谱，返回 (content, finish_reason, usage)。"""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = np.urllib.request.Request(
        np.ZHIPU_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {np.ZHIPU_API_KEY}"},
    )
    with np.urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ch = (data.get("choices") or [{}])[0]
    return (ch.get("message", {}).get("content", ""),
            ch.get("finish_reason"), data.get("usage", {}))


def stats(items):
    dl = [len(x.get("desc", "")) for x in items]
    tl = [len(x.get("title", "")) for x in items]
    if not dl:
        return "（无条目）"
    return (f"n={len(items)} | 标题均 {sum(tl)/len(tl):.0f} 字 | "
            f"正文均 {sum(dl)/len(dl):.0f} 字 | 正文区间 {min(dl)}-{max(dl)}")


# ---------- 三种提示词变体 ----------
def v_current(mat, day_cn):
    """V1：当前线上用的完整提示词（写作标准 + 硬性要求 15 条 + 24 条素材带摘要）"""
    return np.build_prompt(mat, day_cn, attempt=0)


def v_minimal(mat, day_cn):
    """V2：极简提示词 —— 只保留任务与字数要求，去掉全部规则和范例。
    若这个小提示词能拿到 100+ 字，说明是"指令过多把模型绕晕"；
    若仍然只有 40 字，说明是模型能力/输出上限问题。"""
    lines = "\n".join(
        f"{i+1}. {m['title']}\n   来源：{m['source']}\n   URL：{m['url']}\n"
        f"   摘要：{(m.get('summary') or '')[:250]}"
        for i, m in enumerate(mat[:12])
    )
    return f"""从下面素材中挑 8 条 AI 产业新闻，输出 JSON。

素材：
{lines}

要求：
1. 标题 20-30 字，必须含具体主体和数字（如有）。
2. **正文 desc 必须是 100-160 字的一段话，绝不能只写一句话或几十字。**
   内容顺序：谁做了什么 → 具体细节（金额/数量/时间/占比，取自摘要）→ 影响或后续计划。
3. 每条正文写完请自己数一下字数，不足 100 字就继续补充细节。
4. desc 中不得出现素材里没有的数字。

只输出 JSON，格式：
{{"summary":"一句话概括","items":[{{"cat":"policy|tech|industry|capital","title":"...","desc":"...","source":"...","url":"..."}}]}}"""


def v_strict(mat, day_cn):
    """V3：少给素材、只要求 4 条，并强制逐条自检字数。
    用于验证"素材条数 × 输出条数"是否在挤占模型的输出预算。"""
    lines = "\n".join(
        f"{i+1}. {m['title']}\n   来源：{m['source']}\n   URL：{m['url']}\n"
        f"   摘要：{(m.get('summary') or '')[:250]}"
        for i, m in enumerate(mat[:8])
    )
    return f"""从下面素材中挑 4 条 AI 产业新闻，输出 JSON。

素材：
{lines}

**最重要的要求：每条 desc 必须是 140 字左右的完整段落。**
写作结构：主体与动作（20字）+ 关键细节含数字（80字）+ 影响或后续计划（40字）。
写完每条后先数字数，不足 130 字就补细节，宁长勿短。

只输出 JSON：
{{"summary":"一句话","items":[{{"cat":"policy|tech|industry|capital","title":"20-30字标题","desc":"140字左右正文","source":"媒体名","url":"链接"}}]}}"""


VARIANTS = [("V1-当前完整提示词", v_current),
            ("V2-极简提示词", v_minimal),
            ("V3-4条+强制自检", v_strict)]

PROBE_MODELS = ["glm-4-flash", "glm-4-flashx", "glm-4-air", "glm-4-plus",
                "glm-4.5-flash", "glm-4.6"]


def main():
    if not np.ZHIPU_API_KEY:
        print("缺少 ZHIPU_API_KEY")
        return 2

    day = DAY or time.strftime("%Y-%m-%d")
    print(f"===== 诊断日期：{day} =====")
    mat = np.collect_material(hours=72)
    print(f"素材 {len(mat)} 条")
    if len(mat) < 6:
        print("素材不足，退出")
        return 1

    day_cn = f"{int(day[5:7])}月{int(day[8:10])}日"

    # ---------- Q1 + Q2：同一模型 × 三种提示词 ----------
    print("\n" + "=" * 72)
    print("Q1/Q2：glm-4-flash 在三种提示词下的输出长度（含 finish_reason / usage）")
    print("=" * 72)
    for name, fn in VARIANTS:
        prompt = fn(mat, day_cn)
        print(f"\n----- {name}（提示词 {len(prompt)} 字符）-----")
        try:
            content, fr, usage = call("glm-4-flash", prompt)
        except Exception as e:
            print(f"  调用失败: {e}")
            continue
        print(f"  finish_reason={fr} | usage={usage}")
        print(f"  原始输出 {len(content)} 字符，前 120 字：{content[:120]!r}")
        try:
            obj = np.parse_llm_json(content)
            print(f"  → {stats(obj.get('items') or [])}")
            for it in (obj.get("items") or [])[:3]:
                print(f"     · [{it.get('cat')}] {it.get('title','')[:30]}"
                      f" | desc {len(it.get('desc',''))} 字")
        except Exception as e:
            print(f"  JSON 解析失败: {e}")
        time.sleep(2)

    # ---------- Q3：可用模型探测 ----------
    print("\n" + "=" * 72)
    print("Q3：账号可用模型探测（用极简提示词，只问可用性与输出长度）")
    print("=" * 72)
    short_mat = mat[:6]
    p = v_minimal(short_mat, day_cn)
    for m in PROBE_MODELS:
        try:
            content, fr, usage = call(m, p)
            obj = np.parse_llm_json(content)
            print(f"  ✅ {m:<16} finish={fr} usage={usage}")
            print(f"       {stats(obj.get('items') or [])}")
        except Exception as e:
            msg = str(e)
            if hasattr(e, "read"):
                try:
                    msg = e.read().decode()[:200]
                except Exception:
                    pass
            print(f"  ❌ {m:<16} {msg[:180]}")
        time.sleep(2)

    print("\n===== 诊断结束 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
