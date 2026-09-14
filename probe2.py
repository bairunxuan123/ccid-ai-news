#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本 2：在免费模型 glm-4-flash 的约束下，找到能稳定产出 100+ 字正文的调用方式。

前一轮诊断结论：
  · finish_reason=stop（非 length）→ 不是输出 token 上限
  · 完整提示词 → 正文均 51 字；极简提示词 → 71 字；4 条+强制自检 → 56 字
  · glm-4-flashx / glm-4-air / glm-4-plus / glm-4.6 均 code 1113 余额不足，不可用

假设：一次生成 N 条 JSON 时，模型会主动压缩每条的输出配额；
      拆成"单条素材 → 一段正文"的窄任务后应能写长。

本脚本验证：
  T1 单条素材、直接要求 130 字
  T2 单条素材、按三段结构化填空（主体动作/细节/影响）
  T3 单条素材、先列要点再成文（两步式）
  T4 批量 3 条（对照组，看批量是否真的压缩）
  T5 更多免费模型名探测（可能还有其它可用免费模型）
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_pipeline as np  # noqa: E402


def call(model, prompt, temperature=0.5, max_tokens=4096):
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


def raw_src(m):
    """单条素材的原始素材块"""
    return (f"标题：{m['title']}\n"
            f"来源：{m['source']}\n"
            f"原文摘要：{(m.get('summary') or '')[:600]}")


# ---------- T1：直接要求 130 字 ----------
def t1(m):
    return f"""请把下面这条新闻写成一段中文产业动态正文，用于行业简报。

{raw_src(m)}

要求：**正文 120-150 字**，写成一段完整陈述，包含具体的机构名/公司名和摘要中的具体数字。
直接输出正文本身，不要标题、不要解释、不要 markdown。"""


# ---------- T2：三段结构化填空 ----------
def t2(m):
    return f"""请把下面这条新闻写成一段中文产业动态正文。

{raw_src(m)}

请严格按以下三部分依次写出，然后用逗号/句号自然连成**一段话**，总长 **120-150 字**：
第一部分（约 30 字）：谁（具体机构/公司全名）做了什么（发布/融资/推出了什么）
第二部分（约 70 字）：关键细节，必须写出摘要里的具体数字（金额、数量、时间、占比、技术规格）
第三部分（约 40 字）：影响、对比或后续计划

写作纪律：
- 写完请自己数一遍字数，**不足 110 字必须继续补充第二部分的事实细节**。
- 只允许使用摘要中出现过的数字，不得自己编造。
- 直接输出这段正文，不要输出三部分的标题。

正文："""


# ---------- T3：先要点后成文 ----------
def t3(m):
    return f"""下面是一条新闻素材。请分两步处理。

{raw_src(m)}

第一步：在脑中列出 4-6 个事实要点（含摘要里的具体数字）。
第二步：把这些要点写成**一段 120-150 字的连贯中文正文**，用于行业简报。

硬性要求：字数必须达到 120 字以上（这是最重要的要求）；只用摘要中出现过的数字；
不要分点罗列，要写成一段话；不要输出要点列表，只输出最终正文。

正文："""


# ---------- T4：批量 3 条（对照） ----------
def t4(ms):
    mat = "\n\n".join(f"【第{i+1}条】\n{raw_src(m)}" for i, m in enumerate(ms))
    return f"""请把下面 3 条新闻各写成一段中文产业动态正文。

{mat}

要求：每条正文 **120-150 字**，含具体主体与摘要中的数字。
只输出 JSON：{{"items":[{{"title":"20-30字标题","desc":"120-150字正文","source":"媒体名","url":"链接"}}]}}"""


def pick_desc_len(text):
    return len(re.sub(r"\s+", "", text))


def main():
    if not np.ZHIPU_API_KEY:
        print("缺少 ZHIPU_API_KEY")
        return 2
    day = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d")

    print(f"===== 诊断 2：单条调用能否突破 100+ 字（{day}）=====")
    mat = np.collect_material(hours=72)
    print(f"素材 {len(mat)} 条")
    # 挑摘要较长的 6 条做样本（模型有料可写）
    pool = [m for m in mat if len(m.get("summary") or "") >= 200][:6]
    if len(pool) < 4:
        pool = mat[:6]
    print(f"样本 {len(pool)} 条（摘要长度 "
          f"{[len(m.get('summary') or '') for m in pool]}）")

    results = {}
    for tag, fn in (("T1-直接要求130字", t1), ("T2-三段填空", t2), ("T3-先要点后成文", t3)):
        lens, frs = [], []
        print(f"\n----- {tag} -----")
        for m in pool:
            try:
                c, fr, u = call("glm-4-flash", fn(m))
                L = pick_desc_len(c)
                lens.append(L)
                frs.append(fr)
                print(f"   {L:>4} 字 | finish={fr} | {m['title'][:34]}")
            except Exception as e:
                print(f"   调用失败: {str(e)[:120]}")
            time.sleep(1.5)
        if lens:
            results[tag] = lens
            print(f"   → 平均 {sum(lens)/len(lens):.0f} 字 | 区间 {min(lens)}-{max(lens)}"
                  f" | ≥100 字占 {sum(1 for x in lens if x>=100)}/{len(lens)}")

    print("\n----- T4-批量3条（对照） -----")
    try:
        c, fr, u = call("glm-4-flash", t4(pool[:3]))
        obj = np.parse_llm_json(c)
        dl = [len(x.get("desc", "")) for x in (obj.get("items") or [])]
        print(f"   {len(dl)} 条 | 正文 {dl} | 平均 {sum(dl)/max(1,len(dl)):.0f} 字 | finish={fr}")
    except Exception as e:
        print(f"   失败: {str(e)[:200]}")

    print("\n" + "=" * 72)
    print("T5：更多免费模型名探测")
    print("=" * 72)
    test_prompt = t1(pool[0])
    for name in ["glm-4-flash", "glm-4-flash-250414", "glm-z1-flash",
                 "glm-4.5-flash", "glm-4-flashx-250414", "glm-4-air-250414",
                 "glm-4.6-flash", "glm-4v-flash"]:
        try:
            c, fr, u = call(name, test_prompt)
            print(f"  ✅ {name:<22} {pick_desc_len(c)} 字 | finish={fr} | {u}")
        except Exception as e:
            msg = str(e)
            if hasattr(e, "read"):
                try:
                    msg = e.read().decode()[:160]
                except Exception:
                    pass
            print(f"  ❌ {name:<22} {msg[:140]}")
        time.sleep(1.5)

    print("\n===== 诊断 2 结束 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
