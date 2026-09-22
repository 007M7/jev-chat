"""把人工核对过的表格回收成标注集（calibrate.py 可直接吃）。

这是"用真实数据修群聊题目集"的最后一步：上一环（import_app_records.py / make_label_table.py）
生成核对表，用户只在**判错的行**上写正确答案，本脚本把整张表收回来：
填了的用人工值，留空的沿用模型值（工作流的前提就是"留空=模型对"）。

用法:
  python apply_labels.py --table out/group_labels.md --out fixtures/labeled_set_group.json
  python apply_labels.py --table out/group_labels.md --questions one_on_one --out fixtures/x.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

GROUP_FIELDS = {
    "asked_to_me": "bool", "asker_intent": "choice", "need_reply": "bool",
    "best_group_action": "choice", "need_from_me": "choice",
    "topic_closed": "bool", "group_tension": "score",
}
ONE_ON_ONE_FIELDS = {
    "literal_question": "bool", "true_intent": "choice", "danger_level": "score",
    "should_reply_now": "bool", "best_action": "choice", "she_needs": "choice",
    "tension_resolved": "bool",
}

SECTION = re.compile(r"^##\s+\d+\.\s*(.*?)\s*$")
# | 中文标签 `key` | 模型值 | 你的答案 |
ROW = re.compile(r"^\|\s*[^|]*`([a-z_]+)`\s*\|\s*([^|]*)\|\s*([^|]*)\|")


def parse_bool(v: str) -> bool | None:
    s = v.strip()
    if s in ("是", "true", "True", "1"):
        return True
    if s in ("否", "false", "False", "0"):
        return False
    # 模型给的是概率（0~1），按 0.5 划
    try:
        return float(s) >= 0.5
    except ValueError:
        return None


def parse_num(v: str) -> float | None:
    try:
        return float(v.strip())
    except ValueError:
        return None


def parse_transcript(block: str) -> list[list[str]]:
    out: list[list[str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("：", ":"):
            if sep in line:
                who, _, body = line.partition(sep)
                who, body = who.strip(), body.strip()
                if not body:
                    break
                side = "me" if who == "我" else "other"
                out.append([side, body])
                break
    return out


def parse_table(text: str, fields: dict[str, str]) -> list[dict]:
    cases: list[dict] = []
    title = None
    transcript: list[str] = []
    in_block = False
    expect: dict = {}

    def flush() -> None:
        nonlocal expect, transcript
        if title is not None and expect:
            cases.append({
                "id": f"g{len(cases) + 1:02d}",
                "source": title,
                "relationship": "",
                "messages": parse_transcript("\n".join(transcript)),
                "expect": dict(expect),
            })
        expect = {}
        transcript = []

    for raw in text.splitlines():
        line = raw.rstrip()
        m = SECTION.match(line)
        if m:
            flush()
            title = m.group(1)
            continue
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            transcript.append(line)
            continue
        r = ROW.match(line)
        if r:
            key, model_val, human_val = r.group(1), r.group(2), r.group(3)
            if key not in fields:
                continue
            chosen = human_val.strip() or model_val.strip()
            if not chosen or chosen == "（无）":
                continue
            kind = fields[key]
            if kind == "bool":
                v = parse_bool(chosen)
                if v is not None:
                    expect[key] = v
            elif kind == "score":
                v = parse_num(chosen)
                if v is not None:
                    expect[key] = v
            else:
                expect[key] = chosen
    flush()
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="核对表 → 标注集")
    ap.add_argument("--table", required=True, help="人工核对过的 Markdown 表")
    ap.add_argument("--out", required=True, help="输出的标注集 JSON")
    ap.add_argument("--questions", choices=("one_on_one", "group"), default="group")
    args = ap.parse_args()

    p = Path(args.table)
    if not p.exists():
        print(f"找不到 {p}")
        return 2
    text = p.read_text(encoding="utf-8")
    fields = GROUP_FIELDS if args.questions == "group" else ONE_ON_ONE_FIELDS

    cases = parse_table(text, fields)
    cases = [c for c in cases if c["messages"] and c["expect"]]
    if not cases:
        print("没解析出用例。检查表格格式：")
        print("  - 每个用例以 `## N. 标题` 开头")
        print("  - 对话放在 ``` 围栏里，每行形如「我：…」/「昵称：…」")
        print("  - 字段行形如 | 标签 `asked_to_me` | 模型值 | 你的答案 |")
        return 2

    # 完整性检查：缺字段的用例会让指标算不准，如实报出来
    missing: dict[str, int] = {}
    for c in cases:
        for k in fields:
            if k not in c["expect"]:
                missing[k] = missing.get(k, 0) + 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"解析出 {len(cases)} 条用例（题目集 {args.questions}）")
    corrected = sum(1 for c in cases if c.get("source"))
    print(f"  含对话 {sum(1 for c in cases if c['messages'])} 条　写出 {out}")
    if missing:
        print()
        print("  注意：以下字段在部分用例里缺失，会影响对应指标的样本量——")
        for k, n in sorted(missing.items(), key=lambda kv: -kv[1]):
            print(f"    {k}: 缺 {n}/{len(cases)}")
        print("  （表格里留空且模型值为「（无）」的行会缺字段；建议补齐或删掉该用例）")
    print()
    print(f"下一步：python calibrate.py --questions {args.questions} --fixture {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
