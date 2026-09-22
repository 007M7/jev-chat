"""导入 App 导出的记录，产出：可读复盘 / 群聊标注表 / 记忆蒸馏输入。

数据闭环：App 记录 → 导出（「文件」App 里可见）→ PC 侧处理 → 回灌档案。
这条链断在手机上过很久——记录出不来，校准与蒸馏都做不了，所以先把 PC 侧打通。

App 侧的文件（都在 App 沙盒的 Documents 里，通过文件共享可见）：
  jev_history.json   分析记录（每条含对话原文 transcript、七题原始答案 answersSummary）
  jev_profiles.json  会话档案（这个群是什么 / 各人在本群的角色）
  jev_persons.json   人物档案（跨会话通用的身份）

用法:
  python import_app_records.py --export <目录> --review out/app_review.md
  python import_app_records.py --export <目录> --label-table out/group_labels.md
  python import_app_records.py --export <目录> --distill-json out/for_distill.json
  python import_app_records.py --export <目录> --stats          # 只看统计
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

# 群聊题目集字段（与 questions_group.GROUP_QUESTIONS 对应）
GROUP_FIELDS = [
    ("asked_to_me", "是否在问我"),
    ("asker_intent", "对方想要什么"),
    ("need_reply", "需要我回吗"),
    ("best_group_action", "最佳动作"),
    ("need_from_me", "想从我这里要什么"),
    ("topic_closed", "话题收尾了吗"),
    ("group_tension", "群内紧张度"),
]
ONE_ON_ONE_FIELDS = [
    ("literal_question", "是否字面意思"),
    ("true_intent", "真实意图"),
    ("danger_level", "危险等级"),
    ("should_reply_now", "该不该马上回"),
    ("best_action", "最佳动作"),
    ("she_needs", "对方需要什么"),
    ("tension_resolved", "紧张是否解除"),
]


def load_export(directory: Path) -> tuple[list[dict], dict, dict]:
    def read(name: str, default):
        p = directory / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  ! {name} 不是合法 JSON：{exc}")
            return default

    history = read("jev_history.json", [])
    profiles = read("jev_profiles.json", {})
    persons = read("jev_persons.json", {})
    if not isinstance(history, list):
        raise SystemExit("jev_history.json 应是数组")
    return history, profiles if isinstance(profiles, dict) else {}, persons if isinstance(persons, dict) else {}


def parse_time(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def short_time(s: str) -> str:
    d = parse_time(s)
    return d.strftime("%m-%d %H:%M") if d else (s or "")[:16]


def group_by_session(history: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in history:
        out[str(r.get("sessionKey") or "?")].append(r)
    for v in out.values():
        v.sort(key=lambda r: r.get("at") or "", reverse=True)
    return dict(out)


def render_review(history: list[dict], profiles: dict, persons: dict) -> str:
    L: list[str] = []
    L.append("# App 记录复盘")
    L.append("")
    L.append(f"记录 {len(history)} 条，会话 {len(group_by_session(history))} 个。")
    L.append("")
    if persons:
        L.append("## 人物档案（跨会话通用）")
        L.append("")
        L.append("| 名字 | 身份 | 见过 | 别名 |")
        L.append("|---|---|---|---|")
        for p in sorted(persons.values(), key=lambda x: -(x.get("seenCount") or 0)):
            L.append(f"| {p.get('displayName','')} | {p.get('identity') or '（未填）'} | "
                     f"{p.get('seenCount', 0)} | {'、'.join(p.get('aliases') or []) or '—'} |")
        L.append("")
    L.append("---")
    L.append("")

    for key, recs in sorted(group_by_session(history).items(), key=lambda kv: kv[1][0].get("at", ""), reverse=True):
        prof = profiles.get(key, {})
        title = recs[0].get("chatTitle") or prof.get("title") or "（无标题）"
        L.append(f"## {title}")
        L.append("")
        L.append(f"- 会话键：`{key}`　记录 {len(recs)} 条　群聊：{recs[0].get('isGroup')}")
        if prof.get("relationship"):
            L.append(f"- 会话定位：{prof['relationship']}")
        if prof.get("notes"):
            people = "；".join(f"{k}={v}" for k, v in prof["notes"].items() if k != "group")
            if people:
                L.append(f"- 各人角色：{people}")
        L.append("")
        for r in recs:
            picked = r.get("pickedIndex")
            L.append(f"### {short_time(r.get('at',''))}　{r.get('dangerLabel','')} "
                     f"{r.get('danger', 0):.0f}/9　{r.get('intentLabel','')} "
                     f"{int(round((r.get('intentConfidence') or 0) * 100))}%")
            L.append("")
            L.append(f"- 建议动作：{r.get('actionAdvice','')}"
                     + ("　**（快速模式，未生成候选）**" if r.get("fastMode") else ""))
            L.append(f"- 耗时：感知 {r.get('perceptionSeconds', 0):.1f}s / 总 {r.get('totalSeconds', 0):.1f}s")
            if r.get("speaker"):
                L.append(f"- 发言人：{r['speaker']}")
            if r.get("contextNotes"):
                L.append(f"- 语境提示：{'；'.join(r['contextNotes'])}")
            cands = r.get("candidates") or []
            for i, c in enumerate(cands):
                mark = "　← 你选了这条" if picked == i else ""
                L.append(f"    - #{i + 1} {c}{mark}")
            tr = r.get("transcript") or []
            if tr:
                L.append("")
                L.append("  对话：")
                L.append("")
                L.append("  ```")
                for line in tr[-8:]:
                    L.append(f"  {line}")
                L.append("  ```")
            L.append("")
        L.append("---")
        L.append("")
    return "\n".join(L)


def render_label_table(history: list[dict]) -> str:
    """生成群聊题目集的标注核对表：模型值已有，用户只填错的。"""
    L: list[str] = []
    L.append("# 群聊判断核对表（从 App 记录生成）")
    L.append("")
    L.append("规则：**模型判断对的行留空**；判错的行在「你的答案」列写正确的值。")
    L.append("")
    L.append("可填值：")
    L.append("- 是否类：`是` / `否`")
    L.append("- asker_intent：`ask_info` / `ask_resource` / `share_news` / `express_feeling` / "
             "`give_feedback` / `close_topic`")
    L.append("- best_group_action：`answer_directly` / `share_link` / `promise_and_follow` / "
             "`acknowledge_brief` / `ask_clarify` / `take_private` / `no_reply`")
    L.append("- need_from_me：`info` / `resource` / `recognition` / `nothing`")
    L.append("- group_tension：0~9 整数")
    L.append("")
    L.append("---")
    L.append("")
    n = 0
    for key, recs in group_by_session(history).items():
        for r in recs:
            ans = r.get("answersSummary") or {}
            if not ans:
                continue
            is_group_set = any(k in ans for k, _ in GROUP_FIELDS)
            if not is_group_set:
                continue                      # 一对一的答案对群聊校准没用，跳过
            n += 1
            L.append(f"## {n}. {r.get('chatTitle','')}　`{short_time(r.get('at',''))}`")
            L.append("")
            L.append("```")
            for line in (r.get("transcript") or [])[-8:]:
                L.append(line)
            L.append("```")
            L.append("")
            L.append("| 字段 | 模型判断 | 你的答案（错才填） |")
            L.append("|---|---|---|")
            for k, label in GROUP_FIELDS:
                L.append(f"| {label} `{k}` | {ans.get(k, '（无）')} |  |")
            L.append("")
            L.append("备注：")
            L.append("")
            L.append("---")
            L.append("")
    if n == 0:
        L.append("**没有可用于群聊校准的记录**：记录里没有群聊题目集的原始答案。")
        L.append("")
        L.append("原因通常是：这批记录是「一对一」题目集跑出来的（对群聊校准没用），")
        L.append("或者用的是更早、还没记录原始答案的版本。")
        L.append("")
        L.append("做法：在 App 里确认「只跟随这个会话」选中目标群聊，再聊几句生成新记录。")
    return "\n".join(L)


def _split_line(line: str) -> dict:
    """把「我：…」/「昵称：…」拆成结构化消息（memory.distill 直接吃这种）。"""
    for sep in ("：", ":"):
        if sep in line:
            who, _, body = line.partition(sep)
            who = who.strip()
            body = body.strip()
            if who == "我":
                return {"side": "me", "text": body, "sender": None}
            if who in ("对方", ""):
                return {"side": "other", "text": body, "sender": None}
            return {"side": "other", "text": body, "sender": who}
    return {"side": "other", "text": line.strip(), "sender": None}


def render_distill_input(history: list[dict]) -> dict:
    """整理成记忆蒸馏的输入：按会话聚合对话原文（结构化，便于 memory.py 直接用）。"""
    out: dict = {"sessions": []}
    for key, recs in group_by_session(history).items():
        seen: list[tuple] = []
        for r in recs:
            for line in r.get("transcript") or []:
                m = _split_line(line)
                sig = (m["side"], m["sender"], m["text"])
                if sig not in seen:
                    seen.append(sig)
        if not seen:
            continue
        out["sessions"].append({
            "session_key": key,
            "title": recs[0].get("chatTitle"),
            "is_group": recs[0].get("isGroup"),
            "messages": [{"side": s, "sender": sn, "text": t} for s, sn, t in seen[-40:]],
            "note": "交给 memory.py --from-export 用；messages 已结构化（side/sender/text）",
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="导入 App 导出的记录")
    ap.add_argument("--export", required=True, help="App 导出的文件所在目录")
    ap.add_argument("--review", help="写出可读复盘（Markdown）")
    ap.add_argument("--label-table", help="写出群聊标注核对表（Markdown）")
    ap.add_argument("--distill-json", help="写出记忆蒸馏输入（JSON）")
    ap.add_argument("--stats", action="store_true", help="只打印统计")
    args = ap.parse_args()

    d = Path(args.export)
    if not d.is_dir():
        print(f"目录不存在：{d}")
        return 2
    history, profiles, persons = load_export(d)
    if not history:
        print(f"{d} 里没有读到记录（需要 jev_history.json）")
        print("从 App 里导出：文件 App → 我的 iPhone → Jev 助手 → 拷出 jev_*.json")
        return 2

    sessions = group_by_session(history)
    print(f"记录 {len(history)} 条　会话 {len(sessions)} 个　人物档案 {len(persons)} 条")
    print()
    for key, recs in sorted(sessions.items(), key=lambda kv: kv[1][0].get("at", ""), reverse=True):
        r0 = recs[0]
        picked = sum(1 for r in recs if r.get("pickedIndex") is not None)
        per = [r.get("perceptionSeconds") or 0 for r in recs if r.get("perceptionSeconds")]
        tot = [r.get("totalSeconds") or 0 for r in recs if r.get("totalSeconds")]
        avg = f"感知均 {sum(per)/len(per):.1f}s / 总均 {sum(tot)/len(tot):.1f}s" if per and tot else "无耗时数据"
        print(f"  {str(r0.get('chatTitle'))[:24]:<26} 记录 {len(recs):>3}  你选过候选 {picked} 条  {avg}")
    with_ans = sum(1 for r in history if r.get("answersSummary"))
    with_tr = sum(1 for r in history if r.get("transcript"))
    print()
    print(f"含原始答案 {with_ans}/{len(history)}　含对话原文 {with_tr}/{len(history)}")
    if with_ans < len(history):
        print("  （缺原始答案的多是更早版本产生的记录；校准需要它，重新跑一批即可）")

    if args.review:
        out = Path(args.review); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_review(history, profiles, persons), encoding="utf-8")
        print(f"\n复盘已写 {out}")

    if args.label_table:
        out = Path(args.label_table); out.parent.mkdir(parents=True, exist_ok=True)
        md = render_label_table(history)
        out.write_text(md, encoding="utf-8")
        usable = md.count("| 你的答案（错才填） |")
        print(f"\n标注表已写 {out}（可核对的记录 {usable} 条）")

    if args.distill_json:
        out = Path(args.distill_json); out.parent.mkdir(parents=True, exist_ok=True)
        payload = render_distill_input(history)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n蒸馏输入已写 {out}（会话 {len(payload['sessions'])} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
