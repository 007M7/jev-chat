"""端到端：聊天截图 → 判断 → 起草候选 → 排序。

每一步都沿用安卓版已有的实现，不另起一套：
  感知  vlm（默认，多模态视觉模型）或 local（本地 OCR + 启发式，离线兜底）
  判断  tools/jev 的 7 道题，走 providers.json 的 roles.judge（默认 TypeSafe 直连）
  起草  providers.json 的 roles.analysis（默认本地 DeepSeek，OpenAI 兼容协议）
  排序  best_reply 单题，同样走 roles.judge

端点与模型全部来自 tools/jev/providers.json，代码里不写死任何一家。
密钥只从环境变量读。

用法:
  python pipeline.py --image shot.png                              # 默认 vlm + 你配置的 provider
  python pipeline.py --image shot.png --perceiver local --perceive-only   # 离线，只做感知
  python pipeline.py --image shot.png --candidates "候选1|候选2|候选3"      # 跳过起草
  python pipeline.py --image shot.png --judge-provider openrouter_jev      # 临时切换判断 provider
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
JEV_DIR = REPO / "tools" / "jev"
QUESTIONS_JSON = JEV_DIR / "questions.json"
DEFAULT_RELATIONSHIP = "对方是我的伴侣；from=me 的是我发的，from=other 的是对方发的"

sys.path.insert(0, str(JEV_DIR))
sys.path.insert(0, str(SPIKE))

import providers as pv  # noqa: E402


def load_canonical() -> dict:
    if not QUESTIONS_JSON.exists():
        raise SystemExit(
            f"缺少 {QUESTIONS_JSON}，先跑: python tools/jev/check_questions.py --write"
        )
    return json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 感知


def perceive_local(image: str, rule: str) -> tuple[list[dict], dict]:
    import extract as ex

    snap = ex.extract(image, rule=rule)
    # 本地路径拿不到发言人昵称（OCR 会把昵称当消息或粘进正文），sender 一律为 None
    msgs = [{"side": m.side, "text": m.text, "sender": None, "kind": "text"} for m in snap.messages]
    return msgs, {
        "perceiver": "local",
        "rule": rule,
        "title": snap.title,
        "n": len(msgs),
        "note": "本地启发式：群聊昵称会被当成消息、emoji 会丢失、深色模式无颜色线索。"
                "实测桌面深色截图：10 条里 4 条是昵称，且有一处正文丢失。",
    }


def perceive_vlm(
    image: str, model: str | None, provider: str | None, include_nontext: bool = True
) -> tuple[list[dict], dict]:
    import vlm_extract as vx

    structured, meta = vx.extract_vlm(image, model=model, include_nontext=include_nontext, provider=provider)
    msgs = vx.to_jev_messages(structured, include_nontext)
    senders = vx.collect_senders(msgs)
    return msgs, {
        "perceiver": "vlm",
        "provider": meta["provider"],
        "model": meta["model"],
        "elapsed_s": meta["elapsed_s"],
        "cost_usd": meta["cost_usd"],
        "usage": meta["usage"],
        "title": structured.get("title"),
        "is_group": structured.get("is_group"),
        "senders": senders,
        "structured": structured,
        "n": len(msgs),
    }


# ---------------------------------------------------------------- 判断 / 起草 / 排序


def to_python_state(
    messages: list[dict],
    relationship: str,
    memory_brief: dict | None = None,
    is_group: bool = False,
    senders: list[str] | None = None,
) -> dict:
    """把感知结果转成判断层的 state，并把 memory 注入进去。

    messages 是 [{"side": "me"|"other", "text": str, "sender": str|None}]。
    群聊走 questions_group.build_group_state：它保留每条消息的 sender，
    判断层因此能区分"谁说的"，而不是把所有群成员当成同一个人。
    一对一走 questions.build_state：那里词表是 her/me，必须显式映射，
    否则直接抛 ValueError（两端词表不一致是历史遗留）。
    """
    if is_group:
        import questions_group as qg

        state = qg.build_group_state(messages, relationship, senders)
    else:
        import questions as q

        mapped = [
            ("me" if m["side"] == "me" else "her", m["text"]) for m in messages
        ]
        state = q.build_state(mapped, relationship)
    if memory_brief:
        state["chat"]["memory"] = memory_brief
    return state


def pick_profile(canon: dict, is_group: bool) -> tuple[str, dict]:
    """按场景选题目档案。返回 (档案名, 档案内容)。

    这一步是本轮修的**产品缺陷**所在：一对一那套题目预设了"对方在测试你在不在乎"，
    用在群聊上会产出「要你办事」+「需要行动」+「不必给实质内容」这类自相矛盾的组合
    （实测：标注集 30 条一对一场景跨题矛盾 0%，真实群聊截图出现 1 处）。
    """
    profiles = canon.get("profiles") or {}
    name = "group" if is_group else "one_on_one"
    prof = profiles.get(name)
    if prof:
        return name, prof
    # 兼容没有 profiles 的旧 questions.json
    return "one_on_one", {
        "label": "一对一（旧版 JSON）",
        "calibrated": True,
        "judge_questions": canon["judge_questions"],
        "rank_question": canon["rank_question"],
        "relationship_default": DEFAULT_RELATIONSHIP,
    }


def judge(
    state: dict,
    questions: dict,
    timeout: float,
    provider: str | None = None,
) -> tuple[dict, dict]:
    import calibrate as cal
    import jev_client

    t0 = time.time()
    raw = jev_client.ask(state, questions, timeout=timeout, provider=provider)
    elapsed = time.time() - t0
    return cal.answers_of(raw), {
        "elapsed_s": round(elapsed, 2),
        "usage": raw.get("usage") or {},
        "provider": raw.get("provider"),
        "model": raw.get("model"),
    }


def parse_three(content: str) -> list[str]:
    """与 JevClient.kt 的 parseThree 同策略：先取首个 [ 到末个 ]，失败再按行切。"""
    s = (content or "").strip()
    a, b = s.find("["), s.rfind("]")
    if a >= 0 and b > a:
        try:
            arr = json.loads(s[a : b + 1])
            out = [str(x).strip() for x in arr if str(x).strip()]
            if out:
                return out[:3]
        except json.JSONDecodeError:
            pass
    lines = []
    for ln in s.splitlines():
        ln = ln.strip().lstrip("-*0123456789.、) ").strip().strip('"').strip("'").strip()
        if ln:
            lines.append(ln)
    return lines[:3]


def draft(
    canon: dict,
    messages: list[tuple[str, str]],
    relationship: str,
    timeout: float,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[list[str], dict]:
    d = canon["draft"]
    # 带上发言人：群聊里起草模型也要知道"谁说的"，否则会把不同人的话当成一个人
    def _line(m: dict) -> str:
        who = d["self_label"] if m["side"] == "me" else (m.get("sender") or d["other_label"])
        return f"{who}：{m['text']}"

    convo = "\n".join(_line(m) for m in messages[-int(d["convo_messages"]) :])
    user = d["user_prompt_template"].replace("{relationship}", relationship).replace("{convo}", convo)
    msgs = [
        {"role": "system", "content": d["system_prompt"]},
        {"role": "user", "content": user},
    ]
    t0 = time.time()
    content, usage, pinfo = pv.chat(
        msgs,
        role="analysis",
        provider_override=provider,
        model=model,
        temperature=float(d["temperature"]),
        timeout=timeout,
    )
    elapsed = time.time() - t0
    cands = parse_three(content)
    while len(cands) < 3:
        cands.append("（稍等，我看下）")
    return cands, {
        "provider": pinfo,
        "elapsed_s": round(elapsed, 2),
        "usage": usage,
    }


def rank(
    state: dict,
    candidates: list[str],
    timeout: float,
    provider: str | None = None,
    is_group: bool = False,
) -> tuple[list[tuple[str, float]], dict]:
    import calibrate as cal
    import jev_client
    import questions as q
    import questions_group as qg

    rank_q = qg.build_group_rank_question(candidates) if is_group else q.build_rank_question(candidates)
    t0 = time.time()
    raw = jev_client.ask(state, rank_q, timeout=timeout, provider=provider)
    elapsed = time.time() - t0
    answers = cal.answers_of(raw)
    ans = answers.get("best_reply") or {}
    probs = ans.get("probabilities") or {}
    keys = ("reply_a", "reply_b", "reply_c")
    scored = [(candidates[i], float(probs.get(k, 0.0))) for i, k in enumerate(keys)]
    scored.sort(key=lambda x: -x[1])
    return scored, {
        "elapsed_s": round(elapsed, 2),
        "usage": raw.get("usage") or {},
        "picked": ans.get("choice"),
    }


# ---------------------------------------------------------------- 展示


def show_answer(ans: dict) -> str:
    if "noul" in ans:
        v = float(ans.get("noul") or 0)
        return f"{'是' if v >= 0.5 else '否'}（{v:.2f}）"
    if "choice" in ans:
        conf = ans.get("confidence")
        conf_s = f"（把握 {conf:.0%}）" if isinstance(conf, (int, float)) else ""
        return f"{ans.get('choice')}{conf_s}"
    if "score" in ans:
        probs = ans.get("probabilities") or {}
        try:
            idx = sum(int(k) * float(p) for k, p in probs.items()) if probs else float(ans["score"])
        except (TypeError, ValueError):
            idx = float(ans.get("score") or 0)
        legend = ans.get("legend") or {}
        desc = legend.get(str(round(idx))) or ""
        return f"{idx:.1f}/9" + (f"  {desc[:44]}" if desc else "")
    return str(ans)


def main() -> int:
    ap = argparse.ArgumentParser(description="截图 → 判断 → 起草 → 排序")
    ap.add_argument("--image", required=True)
    ap.add_argument("--perceiver", default="vlm", choices=("vlm", "local"))
    ap.add_argument("--rule", default="color", help="本地感知的归属规则")
    ap.add_argument("--vision-provider", default=None, help="覆盖 roles.perception")
    ap.add_argument("--vision-model", default=None, help="覆盖 roles.perception 的模型")
    ap.add_argument("--judge-provider", default=None, help="覆盖 roles.judge")
    ap.add_argument("--analysis-provider", default=None, help="覆盖 roles.analysis")
    ap.add_argument("--analysis-model", default=None, help="覆盖 roles.analysis 的模型")
    ap.add_argument("--relationship", default=None,
                    help="关系描述；不传则用所选题目档案的默认措辞，档案里的身份信息会叠加进去")
    ap.add_argument("--group", action="store_true",
                    help="强制按群聊处理（本地感知判不出群聊时需要）")
    ap.add_argument("--memory", dest="memory", action="store_true", default=True,
                    help="读取并注入长期 memory 档案（默认开）")
    ap.add_argument("--no-memory", dest="memory", action="store_false",
                    help="不注入 memory")
    ap.add_argument("--memory-distill", action="store_true",
                    help="分析完把这次对话蒸馏成记忆写进档案（会产生一次额外调用）")
    ap.add_argument("--memory-dir", default=None, help="档案目录")
    ap.add_argument("--memory-provider", default=None, help="蒸馏用的 provider")
    ap.add_argument("--candidates", help="固定候选，用 | 分隔，跳过起草")
    ap.add_argument("--perceive-only", action="store_true", help="只做感知，不联网")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    canon = load_canonical()
    print(f"图: {args.image}")
    print(f"provider 配置: {pv.PROVIDERS_JSON}")
    print(f"  {pv.describe('judge', args.judge_provider)}")
    print(f"  {pv.describe('analysis', args.analysis_provider)}")
    if args.perceiver == "vlm":
        print(f"  {pv.describe('perception', args.vision_provider)}")
    print("=" * 70)

    if args.perceiver == "vlm":
        msgs, pmeta = perceive_vlm(args.image, args.vision_model, args.vision_provider)
        u = pmeta.get("usage") or {}
        cost = pmeta.get("cost_usd")
        print(f"感知: vlm  标题 {pmeta.get('title')!r}  群聊 {pmeta.get('is_group')}")
        print(
            f"耗时 {pmeta.get('elapsed_s')}s   token {u.get('prompt_tokens')}/{u.get('completion_tokens')}"
            f"   花费 {('$%.6f' % cost) if cost is not None else '未知'}"
        )
    else:
        msgs, pmeta = perceive_local(args.image, args.rule)
        print(f"感知: local  标题 {pmeta.get('title')!r}")
        print(f"注意: {pmeta['note']}")

    print(f"\n对话（{len(msgs)} 条）" + (f"  发言人 {len(pmeta.get('senders') or [])} 位: {pmeta.get('senders')}" if pmeta.get("senders") else "") + ":")
    for m in msgs:
        who = "我  " if m["side"] == "me" else "对方"
        tag = f"({m['sender']})" if m.get("sender") else ""
        print(f"  {who}{tag} {m['text']}")

    if args.perceive_only:
        print("\n--perceive-only：到此为止，未联网")
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps({"messages": msgs, "perception": pmeta}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 0

    # ---- 场景判定与档案选择（本轮修的产品缺陷：群聊不能套一对一题目）----
    is_group = bool(pmeta.get("is_group")) if args.perceiver == "vlm" else bool(args.group)
    if args.perceiver == "local" and not args.group:
        print("\n注意: 本地感知判不出是不是群聊，按一对一处理。要跑群聊档案请加 --group")
    pname, profile = pick_profile(canon, is_group)
    print("\n" + "=" * 70)
    print(f"题目档案: {pname}  （{profile.get('label')}）"
          + ("" if profile.get("calibrated", True) else "  ⚠ 尚未校准，结论仅供参考"))
    print("=" * 70)

    # ---- memory 档案：把"这个群是什么、说话的人是谁、我答应过什么"变成已知输入 ----
    mem_brief = None
    mem_prof = None
    if args.memory:
        import memory as mem

        title = pmeta.get("title") or "未知会话"
        mpath = Path(args.memory_dir) / f"{mem.slug(title)}.json" if args.memory_dir else mem.DEFAULT_DIR / f"{mem.slug(title)}.json"
        mem_prof = mem.load(mpath)
        if mem_prof.items:
            mem_brief = mem_prof.brief()
            n_live = len(mem_brief.get("facts", {}))
            print(f"memory: 命中档案「{mem_prof.chat_title}」"
                  f"（成员 {len(mem_prof.members)} 人，可用事实 {n_live} 组，分析过 {mem_prof.analyses} 次）")
            if profile.get("calibrated") is False and mem_prof.items:
                print("        档案里的身份信息会优先于群聊的泛化前提")
        else:
            print(f"memory: 没有「{title}」的档案，本次按空档案跑（分析完可用 --memory-distill 建档）")

    # 关系前提：档案里的身份信息优先，其次用档案自带的默认措辞
    relationship = args.relationship or profile.get("relationship_default") or DEFAULT_RELATIONSHIP
    if mem_brief and mem_brief.get("facts", {}).get("identity"):
        identity = "；".join(mem_brief["facts"]["identity"][:3])
        relationship = f"{relationship}。已知身份信息：{identity}"

    senders = list(mem_prof.members) if mem_prof else None
    state = to_python_state(msgs, relationship, mem_brief, is_group, senders)

    questions = profile["judge_questions"]
    print("\n" + "=" * 70)
    print(f"Jev 判断（{len(questions)} 道题，一次请求）")
    print("=" * 70)
    answers, jmeta = judge(state, questions, args.timeout, args.judge_provider)
    ju = jmeta.get("usage") or {}
    print(
        f"model {jmeta.get('model')}   耗时 {jmeta['elapsed_s']}s   "
        f"token {ju.get('input_tokens') or ju.get('prompt_tokens')}/"
        f"{ju.get('output_tokens') or ju.get('completion_tokens')}"
        + (f"   花费 ${ju['cost']:.6f}" if ju.get("cost") is not None else "")
    )
    for k in questions:
        if k in answers:
            print(f"  {k:<18} {show_answer(answers[k])}")

    # 跨题一致性：自相矛盾的结论不该直接显示给用户
    try:
        sys.path.insert(0, str(JEV_DIR))
        import consistency as csx

        pred = {k: (v.get("noul") if "noul" in v else v.get("choice") if "choice" in v else v.get("score"))
                for k, v in answers.items()}
        hits = []
        for name, why, applies, violated in csx.one_on_one_rules() if pname == "one_on_one" else csx.group_rules():
            try:
                if applies(pred) and violated(pred):
                    hits.append(name)
            except Exception:
                continue
        if hits:
            print("\n  ⚠ 跨题矛盾（同一段对话上的答案讲不通，建议人工过一眼）:")
            for h in hits:
                print(f"    ✗ {h}")
        else:
            print("\n  ✓ 跨题一致（答案之间不打架）")
    except Exception as exc:
        print(f"\n  （一致性检查跳过: {exc}）")

    print("\n" + "=" * 70)
    if args.candidates:
        cands = [c.strip() for c in args.candidates.split("|")]
        dmeta = {"provider": {"id": "（固定候选）", "model": None}, "elapsed_s": 0, "usage": {}}
        print("起草候选: 跳过（使用 --candidates 给定的候选）")
    else:
        print("起草候选")
        cands, dmeta = draft(
            canon, msgs, relationship, args.timeout, args.analysis_provider, args.analysis_model
        )
        du = dmeta.get("usage") or {}
        print(
            f"provider {dmeta['provider'].get('id')}  model {dmeta['provider'].get('model')}   "
            f"耗时 {dmeta['elapsed_s']}s   token {du.get('prompt_tokens')}/{du.get('completion_tokens')}"
        )
    for i, c in enumerate(cands, 1):
        print(f"   候选{chr(96 + i)}: {c}")

    print("\n" + "=" * 70)
    print(f"Jev 排序（{pname} 档案）")
    print("=" * 70)
    scored, rmeta = rank(state, cands, args.timeout, args.judge_provider, is_group)
    for i, (text, p) in enumerate(scored, 1):
        print(f"  #{i} · {p:>6.1%}  {text}")
    print(f"\n耗时 {rmeta['elapsed_s']}s   Jev 选中的键: {rmeta.get('picked')}")

    # ---- 写档案：把这次对话蒸馏成长期记忆（要花钱，所以单独开关）----
    if args.memory_distill:
        import memory as mem

        title = pmeta.get("title") or "未知会话"
        mpath = Path(args.memory_dir) / f"{mem.slug(title)}.json" if args.memory_dir else mem.DEFAULT_DIR / f"{mem.slug(title)}.json"
        base = mem.load(mpath)
        base, mmeta = mem.distill(title, msgs, base, args.memory_provider or args.analysis_provider)
        base.is_group = is_group
        mem.save(base, mpath)
        print(f"\nmemory 蒸馏: 新增 {mmeta['added']}  更新 {mmeta['updated']}  "
              f"丢弃(无出处) {mmeta['dropped']}  耗时 {mmeta['elapsed_s']}s")
        print(f"  档案已写 {mpath}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "image": args.image,
                    "profile": pname,
                    "is_group": is_group,
                    "perception": pmeta,
                    "memory": mem_brief,
                    "messages": msgs,
                    "answers": answers,
                    "candidates": cands,
                    "ranked": [{"text": t, "p": p} for t, p in scored],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已写 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
