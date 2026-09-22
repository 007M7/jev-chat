"""把一批真实对话整理成「待人工核对的表格」——群聊题目集校准的前置步骤。

背景：群聊那套题目（asked_to_me / asker_intent / need_reply / best_group_action /
need_from_me / topic_closed / group_tension）标着 `calibrated: false`，而且已定位一处稳定矛盾
（need_reply=否 却建议 answer_directly）。修它**只能靠真实标注数据**，靠猜改措辞不行。

但让用户从零写标注不现实，所以流程是：
  1. 用户给若干张群聊截图
  2. 我们用当前模型跑一遍，把**模型的判断**填进表格
  3. 用户**只需要在错的行上写他的答案**（对的留空）
  4. 用 apply_labels.py 把表格收回成 labeled_set_group.json，再跑校准

本脚本做第 2 步：把已有的分析结果（pipeline/vlm_extract 的输出 JSON）整理成表格。

用法:
  python make_label_table.py --in ../ios-spike/out --out out/group_review.md
  python make_label_table.py --in a.json b.json --out review.md
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# 要人工核对的字段（群聊档案）。模型值取自 answers，用户值留空待填。
GROUP_FIELDS = [
    ("asked_to_me", "是否在问我", "noul"),
    ("asker_intent", "对方想要什么", "choice"),
    ("need_reply", "需要我回吗", "noul"),
    ("best_group_action", "最佳动作", "choice"),
    ("need_from_me", "想从我这里要什么", "choice"),
    ("topic_closed", "话题收尾了吗", "noul"),
    ("group_tension", "群内紧张度", "score"),
]

NOUL_KEYS = {"asked_to_me", "need_reply", "topic_closed"}


def answer_value(ans: dict) -> str:
    """把一个答案渲染成表格里可读的短值。"""
    if not isinstance(ans, dict):
        return ""
    if "noul" in ans:
        v = float(ans["noul"])
        return f"{'是' if v >= 0.5 else '否'}({v:.2f})"
    if "choice" in ans:
        return str(ans["choice"])
    if "score" in ans:
        probs = ans.get("probabilities") or {}
        try:
            return f"{sum(int(k) * float(p) for k, p in probs.items()):.1f}" if probs else f"{float(ans['score']):.1f}"
        except (TypeError, ValueError):
            return str(ans.get("score", ""))
    return ""


def norm_msg(m) -> dict:
    """消息有两种形态：结构化 dict，或 pipeline 输出的 (side, text[, sender]) 元组/数组。"""
    if isinstance(m, dict):
        return m
    if isinstance(m, (list, tuple)):
        return {
            "side": m[0] if len(m) > 0 else "other",
            "text": m[1] if len(m) > 1 else "",
            "sender": m[2] if len(m) > 2 else None,
        }
    return {"side": "other", "text": str(m)}


def load_conversations(paths: list[Path]) -> list[dict]:
    """兼容 pipeline 输出（含 answers/messages）与 vlm_extract 输出（structured/jev_messages）。"""
    out: list[dict] = []
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue

        title = ""
        is_group = None
        raw_msgs: list = []
        answers: dict = {}

        if "structured" in d and isinstance(d["structured"], dict):
            s = d["structured"]
            title = s.get("title") or ""
            is_group = s.get("is_group")
            raw_msgs = s.get("messages") or []
        if d.get("messages") and isinstance(d["messages"], list):
            raw_msgs = d["messages"]
        if isinstance(d.get("perception"), dict):
            title = d["perception"].get("title") or title
            is_group = d["perception"].get("is_group", is_group)
        if isinstance(d.get("answers"), dict):
            answers = d["answers"]

        msgs = [norm_msg(m) for m in raw_msgs]
        if not msgs:
            continue
        out.append({
            "file": p.name,
            "title": title,
            "is_group": is_group,
            "messages": msgs,
            "answers": answers,
        })
    return out


def answers_profile(answers: dict) -> str:
    """判断这份答案是哪套题目集的产物。

    这很重要：早期跑出来的结果是**一对一题目**的答案（literal_question / true_intent…），
    拿它去核对群聊题目（asked_to_me / asker_intent…）没有意义，必须用群聊档案重跑。
    不标注清楚会导致用户在一张对不上号的表上白填。
    """
    if not isinstance(answers, dict) or not answers:
        return "none"
    keys = set(answers.keys())
    if keys & {k for k, _, _ in GROUP_FIELDS}:
        return "group"
    if keys & {"literal_question", "true_intent", "she_needs", "best_action", "danger_level"}:
        return "one_on_one"
    return "unknown"


def render(convs: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# 群聊判断核对表（只改错的行）")
    lines.append("")
    lines.append("规则：**模型判断对的行留空**；判错的行，在「你的答案」列写上正确的。")
    lines.append("")
    lines.append("可填的值：")
    lines.append("")
    lines.append("- 是 / 否（是否类字段）")
    lines.append("- asker_intent：`ask_info` 问信息 / `ask_resource` 要资源 / `share_news` 分享 / "
                 "`express_feeling` 表达情绪 / `give_feedback` 给反馈 / `close_topic` 收尾")
    lines.append("- best_group_action：`answer_directly` 直接作答 / `share_link` 给资源 / "
                 "`promise_and_follow` 承诺跟进 / `acknowledge_brief` 简短回应 / `ask_clarify` 先问清楚 / "
                 "`take_private` 转私聊 / `no_reply` 不必回")
    lines.append("- need_from_me：`info` 信息 / `resource` 资源 / `recognition` 认同 / `nothing` 不需要")
    lines.append("- group_tension：0~9 的整数")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, c in enumerate(convs, 1):
        prof = answers_profile(c["answers"])
        lines.append(f"## {i}. {c['title'] or '（无标题）'}　`{c['file']}`")
        lines.append("")
        tag = "群聊" if c.get("is_group") else ("单聊" if c.get("is_group") is False else "未知")
        lines.append(f"识别为：**{tag}**　消息 {len(c['messages'])} 条")
        lines.append("")

        if prof == "one_on_one":
            lines.append("> ⚠️ **这份记录里的答案属于「一对一」题目集，不能用于群聊标注。**")
            lines.append("> 请用群聊档案重跑后再核对：")
            lines.append(f"> `python pipeline.py --image <原图> --perceiver vlm --group --json out/{c['file']}`")
            lines.append("")
        elif prof == "none":
            lines.append("> 这份记录里没有判断答案（只有感知结果）。可以当作"
                         "「从零标注」的素材，也可以先跑一遍判断再核对。")
            lines.append("")
        else:
            lines.append("模型答案：**已就绪（群聊题目集）**")
            lines.append("")

        lines.append("对话（最后 8 条）：")
        lines.append("")
        lines.append("```")
        for m in c["messages"][-8:]:
            side = m.get("side", m.get("from", "?"))
            who = "我" if side == "me" else (m.get("sender") or "对方")
            body = m.get("text") or m.get("description") or ""
            lines.append(f"{who}：{body}")
        lines.append("```")
        lines.append("")
        lines.append("| 字段 | 模型判断 | 你的答案（错才填） |")
        lines.append("|---|---|---|")
        for key, label, _kind in GROUP_FIELDS:
            model = answer_value(c["answers"].get(key, {})) if prof == "group" else ""
            lines.append(f"| {label} `{key}` | {model or '（无）'} |  |")
        lines.append("")
        lines.append("补充备注（可选）：")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成群聊判断核对表")
    ap.add_argument("--in", dest="inputs", nargs="+", required=True,
                    help="分析结果 JSON 文件或目录")
    ap.add_argument("--out", default=str(ROOT / "out" / "group_review.md"))
    args = ap.parse_args()

    paths: list[Path] = []
    for item in args.inputs:
        p = Path(item)
        if p.is_dir():
            paths.extend(sorted(Path(x) for x in glob.glob(str(p / "*.json"))))
        else:
            paths.append(p)
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("没有可读的 JSON 文件")
        return 2

    convs = load_conversations(paths)
    if not convs:
        print(f"读了 {len(paths)} 个 JSON，但没有一个含对话内容")
        print("（需要 pipeline.py 或 vlm_extract.py 的输出，它们带 messages/structured）")
        return 2

    md = render(convs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    print(f"读了 {len(paths)} 个 JSON，提取出 {len(convs)} 段对话")
    for c in convs:
        n = sum(1 for k, _, _ in GROUP_FIELDS if c["answers"] and k in c["answers"])
        print(f"  {c['file']:<34} 标题={c['title'] or '(无)':<20} 消息{len(c['messages']):>3} 条  已有模型答案 {n}/7")
    print()
    print(f"核对表已写 {out}")
    print("把它发给用户/自己填：只改错的行，然后把文件放回来跑 apply_labels.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
