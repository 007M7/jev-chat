"""跨题一致性检查：同一段对话上，7 道题的答案之间不许自相矛盾。

为什么需要这个：`calibrate.py` 只把每道题单独和人工标注比，**测不出题目之间打架**。
实测到过这种输出——`she_needs` 说「对方要具体行动（96%）」，`true_intent` 说「要你办事（89%）」，
而 `should_reply_now` 说「不必给实质内容（0.22）」。三道题各自可能都在容差内，合起来却讲不通。
对一个把答案并排显示给用户看的界面来说，这是产品缺陷，不是学术问题。

做法：把题目语义里成立的蕴含关系写成规则，在已有答案上离线检查（不调 API）。
每条规则的违规率按**前置条件成立的用例数**做分母——否则规则从未被触发时也会显示
0% 违规，那是假通过。

数据源默认取 calibrate.py 的输出，所以成本为零。

用法:
  python calibrate.py                # 先产生 report/calibration.json
  python consistency.py              # 检查跨题一致性
  python consistency.py --gate 0.10  # 违规率超 10% 就返回非零
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT = ROOT / "report" / "calibration.json"


def as_bool(v: object) -> bool:
    """noul 题返回的是概率，按 0.5 划。"""
    try:
        return float(v) >= 0.5
    except (TypeError, ValueError):
        return bool(v)


def as_num(v: object) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# (名字, 为什么互斥, 前置条件, 违规判定)
ONE_ON_ONE_RULES: list[tuple[str, str, Callable[[dict], bool], Callable[[dict], bool]]] = [
    (
        "要你办事时不该「什么都不需要」",
        "request_action 是「现在就想要具体行动」，nothing 是「不再需要任何东西」，互斥",
        lambda p: p.get("true_intent") == "request_action",
        lambda p: p.get("she_needs") == "nothing",
    ),
    (
        "需要行动时不该已是「和平收尾」",
        "action 是「还没拿到具体行动」，close_topic 是「已接受、明确不需要更多」，互斥",
        lambda p: p.get("she_needs") == "action",
        lambda p: p.get("true_intent") == "close_topic",
    ),
    (
        "「和平收尾」时不该还需要别的",
        "close_topic 意味着对方已接受，此时 she_needs 只应是 nothing",
        lambda p: p.get("true_intent") == "close_topic",
        lambda p: p.get("she_needs") != "nothing",
    ),
    (
        "紧张已解除时不该仍是高危",
        "tension_resolved=true（已接受/已开玩笑）与 danger_level>=5（在测试你、指责你）不相容",
        lambda p: as_bool(p.get("tension_resolved")),
        lambda p: as_num(p.get("danger_level")) >= 5.0,
    ),
    (
        "对方什么都不需要时不该急着给实质内容",
        "nothing 时没有人在等实质内容，should_reply_now 不该为真",
        lambda p: p.get("she_needs") == "nothing",
        lambda p: as_bool(p.get("should_reply_now")),
    ),
    (
        "对方要行动时不该建议「少说两句」",
        "request_action 与 say_less（保持简短或什么都不加）互斥",
        lambda p: p.get("true_intent") == "request_action",
        lambda p: p.get("best_action") == "say_less",
    ),
    (
        "字面请求下不该「要办事」却「不用给实质内容」",
        "最新消息是字面意思、对方要具体行动、且她需要的正是行动时，"
        "should_reply_now 不该为假。加 literal_question 前置条件是为了把"
        "「对方在试探你记不记得」这类正常情形排除在外",
        lambda p: (
            p.get("true_intent") == "request_action" and p.get("she_needs") == "action"
        ),
        lambda p: as_bool(p.get("literal_question")) and not as_bool(p.get("should_reply_now")),
    ),
]


# 群聊档案的规则。字段名与 questions_group.GROUP_QUESTIONS 对应。
GROUP_RULES: list[tuple[str, str, Callable[[dict], bool], Callable[[dict], bool]]] = [
    (
        "没在问我时不该需要我回复",
        "asked_to_me=false（消息不是对我说的）与 need_reply=true 互斥",
        lambda p: not as_bool(p.get("asked_to_me")),
        lambda p: as_bool(p.get("need_reply")),
    ),
    (
        "话题已收尾时不该还需要回复",
        "topic_closed=true 与 need_reply=true 互斥",
        lambda p: as_bool(p.get("topic_closed")),
        lambda p: as_bool(p.get("need_reply")),
    ),
    (
        "对方已收尾时不该还需要从我这里拿东西",
        "asker_intent=close_topic 意味着对方在收尾，need_from_me 只应是 nothing",
        lambda p: p.get("asker_intent") == "close_topic",
        lambda p: p.get("need_from_me") != "nothing",
    ),
    (
        "对我说且问信息/要资源时不该「什么都不需要」",
        "asker_intent=ask_info/ask_resource 且 asked_to_me=true（这条是对我说的）时，"
        "need_from_me 不可能是 nothing。"
        "**必须有 asked_to_me 这个前提**：群聊里对方想要资源但并没向我要（例如号召全群拉朋友）时，"
        "need_from_me=nothing 是对的——少了这个前提会误报",
        lambda p: as_bool(p.get("asked_to_me"))
        and p.get("asker_intent") in ("ask_info", "ask_resource"),
        lambda p: p.get("need_from_me") == "nothing",
    ),
    (
        "不需要我回复时不该建议「直接作答」",
        "need_reply=false（没人等我回）与 best_group_action=answer_directly（在群里给出答案）互斥；"
        "此时最多只该 acknowledge_brief 或 no_reply",
        lambda p: not as_bool(p.get("need_reply")),
        lambda p: p.get("best_group_action")
        in ("answer_directly", "share_link", "promise_and_follow", "ask_clarify", "take_private"),
    ),
    (
        "要资源时不该建议「不必回」",
        "asker_intent=ask_resource 且 asked_to_me=true 与 best_group_action=no_reply 互斥",
        lambda p: as_bool(p.get("asked_to_me")) and p.get("asker_intent") == "ask_resource",
        lambda p: p.get("best_group_action") == "no_reply",
    ),
    (
        "有人公开质疑时不该「不必回」",
        "group_tension>=7（公开指责/要求交代）时不回应会伤信任",
        lambda p: as_num(p.get("group_tension")) >= 7.0,
        lambda p: p.get("best_group_action") == "no_reply",
    ),
]


def one_on_one_rules() -> list:
    return ONE_ON_ONE_RULES


def group_rules() -> list:
    return GROUP_RULES


def rules_for(profile: str) -> list:
    return GROUP_RULES if profile == "group" else ONE_ON_ONE_RULES


# 每套档案的字段名。数据源缺字段时必须拒绝——否则 `not as_bool(None)` 会变成 True，
# 规则被"假触发"，算出来一个没有意义的 0% 违规率（实测踩到过）。
PROFILE_FIELDS = {
    "one_on_one": (
        "literal_question", "true_intent", "danger_level",
        "should_reply_now", "best_action", "she_needs", "tension_resolved",
    ),
    "group": (
        "asked_to_me", "asker_intent", "group_tension",
        "need_reply", "best_group_action", "need_from_me", "topic_closed",
    ),
}


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"找不到 {path}，先跑 python calibrate.py")
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise SystemExit(f"{path.name} 结构不对，找不到 cases 列表")
    return [c for c in cases if c.get("predicted")]


def main() -> int:
    ap = argparse.ArgumentParser(description="跨题一致性检查（离线，不调 API）")
    ap.add_argument("--report", default=str(DEFAULT_REPORT), help="calibrate.py 的输出")
    ap.add_argument("--profile", default="one_on_one", choices=("one_on_one", "group"),
                    help="用哪套规则（字段名不同）")
    ap.add_argument("--gate", type=float, default=None, help="违规率上限，超过返回非零")
    ap.add_argument("--show", type=int, default=8, help="每条规则最多列几个违规用例 id")
    args = ap.parse_args()

    cases = load_cases(Path(args.report))

    # 字段匹配校验：档案和数据的题目集必须对得上
    fields = PROFILE_FIELDS[args.profile]
    matched = [c for c in cases if all(f in c["predicted"] for f in fields)]
    if not matched:
        print(f"数据源: {Path(args.report).name}    用例数: {len(cases)}")
        print(f"\n拒绝检查：这份数据里没有 {args.profile} 档案的字段。")
        print(f"  需要字段: {', '.join(fields)}")
        print(f"  数据里实际有: {', '.join(sorted(cases[0]['predicted'].keys())) if cases else '(空)'}")
        print(f"\n换用匹配的档案（--profile {'group' if args.profile == 'one_on_one' else 'one_on_one'}），")
        print("或者先按该档案跑一遍（pipeline.py --group）。")
        return 2
    if len(matched) < len(cases):
        print(f"注意: {len(cases) - len(matched)} 条用例缺字段，已跳过")

    cases = matched
    print(f"数据源: {Path(args.report).name}    用例数: {len(cases)}    规则档案: {args.profile}")
    print()
    hdr = f"{'规则':<40}{'触发':>6}{'违规':>6}{'违规率':>9}"
    print(hdr)
    print("-" * 66)

    total_triggered = 0
    total_bad = 0
    details: list[str] = []
    for name, why, applies, violated in rules_for(args.profile):
        triggered = [c for c in cases if _safe(applies, c["predicted"])]
        bad = [c for c in triggered if _safe(violated, c["predicted"])]
        total_triggered += len(triggered)
        total_bad += len(bad)
        rate = len(bad) / len(triggered) if triggered else 0.0
        label = name if len(name) <= 38 else name[:36] + ".."
        print(f"{label:<40}{len(triggered):>6}{len(bad):>6}{rate * 100:>8.1f}%")
        if bad:
            ids = ", ".join(str(c.get("id")) for c in bad[: args.show])
            more = " …" if len(bad) > args.show else ""
            details.append(f"  ■ {name}\n     为什么算矛盾: {why}\n     违规用例: {ids}{more}")

    print("-" * 66)
    rate = total_bad / total_triggered if total_triggered else 0.0
    print(f"总违规 {total_bad} / 触发 {total_triggered} = {rate * 100:.1f}%")
    if details:
        print("\n违规明细:")
        for d in details:
            print(d)

    if args.gate is not None:
        ok = rate <= args.gate
        print(f"\n门禁: 违规率 <= {args.gate * 100:.0f}% → {'通过' if ok else '未通过'}")
        return 0 if ok else 1
    return 0


def _safe(fn: Callable[[dict], bool], pred: dict) -> bool:
    try:
        return bool(fn(pred))
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
