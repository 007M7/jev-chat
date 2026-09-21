"""题目集单一来源 + 三端一致性校验。

问题：7 道判断题 + 排序题 + 起草 prompt 现在有两份手工同步的副本
（Python tools/jev/questions.py、Kotlin JevQuestions.kt + JevClient.kt），
iOS/Swift 会是第三份。手工同步必然漂移——已知 Kotlin 与 Python 之间已经有
3 处标点差异（Python 用 em dash，Kotlin 用连字符），而且 state 里 from 的
取值词表两端不一致（Python 校验 her/me，Kotlin 用 me/other）。

做法：
  questions.json 是唯一来源；各端都必须与它一致。
  --write  从 questions.py + JevClient.kt 导出 questions.json（生成，不手抄）
  默认      校验 questions.json ↔ questions.py ↔ JevQuestions.kt / JevClient.kt

用法:
  python check_questions.py --write      # 导出/刷新单一来源
  python check_questions.py              # 校验（不一致返回 1）
  python check_questions.py --strict     # 连标点差异也算不一致
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
REPO = ROOT.parent.parent
QUESTIONS_JSON = ROOT / "questions.json"
QUESTIONS_PY = ROOT / "questions.py"
JEV_QUESTIONS_KT = REPO / "app/src/main/java/com/jev/probe/jev/JevQuestions.kt"
JEV_CLIENT_KT = REPO / "app/src/main/java/com/jev/probe/jev/JevClient.kt"

# state 里 from 的词表：这是已知的两端不一致点，明写出来让使用者看到
FROM_VOCAB = {
    "python_values": ["her", "me"],
    "app_values": ["me", "other"],
    "map_app_to_python": {"me": "me", "other": "her"},
}


# ---------------------------------------------------------------- 归一化


def normalize(s: str) -> str:
    """把两端写法差异抹平后再比：换行转义、空白、破折号。"""
    s = s.replace("\\n", "\n")
    s = s.replace("\u2014", "-").replace("\u2013", "-")  # em dash / en dash → hyphen
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def compare(a: str, b: str) -> tuple[bool, bool]:
    """返回 (归一化后相等, 原文完全相等)。"""
    return normalize(a) == normalize(b), a == b


# ---------------------------------------------------------------- Kotlin 侧解析


def kotlin_literals_concat(text: str, anchor: str) -> str:
    """从 anchor 起抓连续的 Kotlin 字符串字面量拼接（"a" + "b"），返回合并文本。"""
    i = text.find(anchor)
    if i < 0:
        raise SystemExit(f"在 Kotlin 文件里找不到锚点: {anchor!r}")
    j = text.find('"', i)
    if j < 0:
        raise SystemExit(f"锚点后没有字符串字面量: {anchor!r}")
    out: list[str] = []
    pos = j
    while True:
        m = re.compile(r'"((?:[^"\\]|\\.)*)"').match(text, pos)
        if not m:
            break
        out.append(m.group(1))
        pos = m.end()
        gap = re.compile(r'(\s*\+\s*)(")').match(text, pos)
        if gap:
            pos = gap.end() - 1
            continue
        break
    joined = "".join(out)
    return (
        joined.replace("\\n", "\n")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def kotlin_question_strings(path: Path) -> set[str]:
    """把 Kotlin 文件里所有字符串字面量拼成集合（含拼接），用于逐条比对。"""
    text = path.read_text(encoding="utf-8")
    # 先把 "a" + "b" 粘起来，再抽取全部字面量
    glued = re.sub(r'"\s*\+\s*"', "", text)
    lits = re.findall(r'"((?:[^"\\]|\\.)*)"', glued)
    out = set()
    for lit in lits:
        s = lit.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        if len(s) > 3:
            out.add(s)
    return out


def kotlin_match_kind(kt_literals: set[str], expected: str) -> str:
    """返回 "exact"（逐字一致）/ "normalized"（仅抹平空白或破折号后一致）/ "none"。

    必须把这两种分开：只按归一化比较会把真实的标点差异咽掉，
    校验器就变成了永远通过的空壳。

    刻意不做"子串包含"兜底：那会让"Kotlin 端被删掉半句话"变成通过
    （被删短的句子正好是全文的子串）。反向测试实测过这个假阴性。
    """
    for lit in kt_literals:
        if lit == expected:
            return "exact"
    for lit in kt_literals:
        if normalize(lit) == normalize(expected):
            return "normalized"
    return "none"


# ---------------------------------------------------------------- 导出


def python_questions() -> dict:
    sys.path.insert(0, str(ROOT))
    import questions as q  # noqa: E402

    return q.JUDGE_QUESTIONS


def python_rank_template() -> dict:
    sys.path.insert(0, str(ROOT))
    import questions as q  # noqa: E402

    built = q.build_rank_question(["A", "B", "C"])["best_reply"]
    return {
        "key": "best_reply",
        "type": "choice",
        "instructions": built["instructions"],
        "option_keys": ["reply_a", "reply_b", "reply_c"],
        "criteria_vocabulary": "option key -> 候选回复的中文原文（唯一允许 criteria 用中文的地方）",
    }


def draft_prompt_from_kotlin() -> dict:
    sys_lit = kotlin_literals_concat(JEV_CLIENT_KT.read_text(encoding="utf-8"), "val sys = ")
    user_lit = kotlin_literals_concat(JEV_CLIENT_KT.read_text(encoding="utf-8"), "val user = ")
    return {
        "system_prompt": sys_lit,
        "user_prompt_template": (
            user_lit.replace("$relationship", "{relationship}").replace("$convo", "{convo}")
        ),
        "convo_line_template": "{speaker}：{text}",
        "self_label": "我",
        "other_label": "对方",
        "temperature": 0.8,
        "default_model": "deepseek/deepseek-chat-v3.1",
        "max_reply_chars": 40,
        "convo_messages": 10,
        "note": "prompt 取自 app/.../jev/JevClient.kt 的 generateCandidates，逐字一致",
    }


def group_questions() -> dict:
    sys.path.insert(0, str(ROOT))
    import questions_group as g  # noqa: E402

    return g.GROUP_QUESTIONS


def group_rank_template() -> dict:
    sys.path.insert(0, str(ROOT))
    import questions_group as g  # noqa: E402

    built = g.build_group_rank_question(["A", "B", "C"])["best_reply"]
    return {
        "key": "best_reply",
        "type": "choice",
        "instructions": built["instructions"],
        "option_keys": ["reply_a", "reply_b", "reply_c"],
        "criteria_vocabulary": "option key -> 候选回复的中文原文（唯一允许 criteria 用中文的地方）",
    }


def build_canonical() -> dict:
    one_on_one = python_questions()
    group = group_questions()
    return {
        "_comment": (
            "唯一来源。Kotlin(JevQuestions.kt/JevClient.kt) / Python(questions.py) / "
            "iOS(Swift) 都必须与它一致，用 check_questions.py 校验。改题目只改本文件再回灌各端。"
        ),
        "version": 2,
        "message_from": {
            "note": "state.chat.messages[].from 的取值。两端词表不同（已知不一致），跨端交换必须映射",
            **FROM_VOCAB,
        },
        "max_messages_in_state": 10,
        "signature_messages": 6,
        # 两套题目档案：按场景选。group 是新增的，尚未校准。
        "profiles": {
            "one_on_one": {
                "label": "一对一关系（已校准）",
                "calibrated": True,
                "selected_when": "感知层判定 is_group=false",
                "relationship_default": (
                    "对方是我的伴侣；from=me 的是我发的，from=other 的是对方发的"
                ),
                "judge_questions": one_on_one,
                "rank_question": python_rank_template(),
            },
            "group": {
                "label": "群聊（尚未校准）",
                "calibrated": False,
                "selected_when": "感知层判定 is_group=true，且档案里没有更具体的身份信息",
                "relationship_default": (
                    "这是多人群聊，from=me 是我发的，from=other 是群成员发的（可能有多个人）。"
                    "不要假设我和发言者之间有亲密关系，也不要按「对方在测试我在不在乎」解读。"
                ),
                "judge_questions": group,
                "rank_question": group_rank_template(),
            },
        },
        # 兼容旧路径：judge_questions 等价于 profiles.one_on_one
        "judge_questions": one_on_one,
        "rank_question": python_rank_template(),
        "draft": draft_prompt_from_kotlin(),
        "perception": {
            "note": "iOS 感知层：优先多模态视觉模型，本地 Vision OCR 退为离线兜底",
            "vlm_default_model": "qwen/qwen3-vl-32b-instruct",
            "vlm_cheap_model": "qwen/qwen3-vl-8b-instruct",
        },
    }


# ---------------------------------------------------------------- 校验


def verify(strict: bool) -> int:
    if not QUESTIONS_JSON.exists():
        print(f"缺少 {QUESTIONS_JSON.name}，先跑 python check_questions.py --write")
        return 2
    canon = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    py_q = python_questions()
    kt_lits = kotlin_question_strings(JEV_QUESTIONS_KT)

    problems: list[str] = []
    dash_only: list[str] = []
    checked = 0

    def record(label: str, kind: str, expected: str) -> None:
        """把 Kotlin 侧的比对结果分类记账。"""
        if kind == "none":
            problems.append(f"{label} 在 Kotlin 里找不到对应文本: {expected[:56]}...")
        elif kind == "normalized":
            dash_only.append(f"{label}: {expected[:44]}...")

    # 1) questions.json ↔ questions.py（逐字）
    jq = canon["judge_questions"]
    if list(jq.keys()) != list(py_q.keys()):
        problems.append(f"题目键/顺序不一致: json={list(jq.keys())} py={list(py_q.keys())}")
    for key, item in py_q.items():
        if key not in jq:
            problems.append(f"json 缺题目 {key}")
            continue
        checked += 1
        if item["type"] != jq[key]["type"]:
            problems.append(f"{key}.type: json={jq[key]['type']} py={item['type']}")
        if not item["instructions"] == jq[key]["instructions"]:
            problems.append(f"{key}.instructions 与 json 逐字不一致")
        if isinstance(item["criteria"], dict):
            if list(item["criteria"].keys()) != list(jq[key]["criteria"].keys()):
                problems.append(f"{key}.criteria 键不一致")
            for ck, cv in item["criteria"].items():
                if jq[key]["criteria"].get(ck) != cv:
                    problems.append(f"{key}.criteria[{ck}] 与 json 逐字不一致")
        else:
            if list(item["criteria"]) != list(jq[key]["criteria"]):
                problems.append(f"{key}.criteria 分档不一致")

    # 2) questions.json ↔ Kotlin（区分逐字/仅归一化）
    for key, item in jq.items():
        checked += 1
        record(f"{key}.instructions", kotlin_match_kind(kt_lits, item["instructions"]), item["instructions"])
        crit = item["criteria"]
        vals = crit.values() if isinstance(crit, dict) else crit
        for v in vals:
            checked += 1
            record(f"{key}.criteria", kotlin_match_kind(kt_lits, v), v)

    # 3) 起草 prompt：json ↔ Kotlin 原始拼接
    d = canon["draft"]
    kc = JEV_CLIENT_KT.read_text(encoding="utf-8")
    kt_sys = kotlin_literals_concat(kc, "val sys = ")
    kt_user = kotlin_literals_concat(kc, "val user = ")
    checked += 2
    if d["system_prompt"] != kt_sys:
        problems.append("draft.system_prompt 与 JevClient.kt 不一致")
    kt_user_tpl = kt_user.replace("$relationship", "{relationship}").replace("$convo", "{convo}")
    if normalize(d["user_prompt_template"]) != normalize(kt_user_tpl):
        problems.append("draft.user_prompt_template 与 JevClient.kt 不一致")

    # 4) 排序题
    rk = canon["rank_question"]
    checked += 2
    if rk["key"] not in ("best_reply",):
        problems.append("rank_question.key 应为 best_reply")
    record("rank_question.instructions", kotlin_match_kind(kt_lits, rk["instructions"]),
           rk["instructions"])

    # 输出
    print(f"校验项: {checked}")
    print(f"题目数: {len(jq)}   排序题: {rk['key']}")
    print(f"from 词表: Python {FROM_VOCAB['python_values']} / App {FROM_VOCAB['app_values']}（跨端需映射）")
    if dash_only:
        print(f"\n仅标点差异 {len(dash_only)} 处（em dash vs 连字符）：")
        for x in dash_only:
            print(f"  - {x}")
    if problems:
        print(f"\n不一致 {len(problems)} 处：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n结论: questions.json / questions.py / JevQuestions.kt / JevClient.kt 四者一致")
    if strict and dash_only:
        print("--strict：标点差异视为失败")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="题目集单一来源与三端一致性校验")
    ap.add_argument("--write", action="store_true", help="从 questions.py + JevClient.kt 导出 questions.json")
    ap.add_argument("--strict", action="store_true", help="标点差异也算不一致")
    args = ap.parse_args()

    if args.write:
        canon = build_canonical()
        QUESTIONS_JSON.write_text(
            json.dumps(canon, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写 {QUESTIONS_JSON}")
        print(f"  题目 {len(canon['judge_questions'])} 道，排序题 {canon['rank_question']['key']}")
        print(f"  起草 prompt {len(canon['draft']['system_prompt'])} 字符（取自 JevClient.kt）")
        return 0
    return verify(args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
