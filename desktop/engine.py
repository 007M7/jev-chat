"""引擎：感知结果 → Jev 判断 → 起草 → Jev 排序，外加会话隔离。

每一步都复用 tools/ios-spike/pipeline.py 里已验证的实现与顺序，不另起一套：
  场景判定/档案选择 -> state 构造（群聊/一对一走不同 build_state）-> 判断 -> 起草 -> 排序

本文件新增的、也是参考项目缺的两件事：
  1. **会话隔离**。参考项目只有一个全局 history，OCR 也不读会话标题，
     切换会话后新消息会追加进同一个缓冲，导致 state 里混两个会话的内容。
     这里用标题做会话键，并在标题变化时清空历史与去重状态。
  2. **标题容错**。视觉模型会把标题读错——实测同一张图两次读出的标题有两个字不同
     （真实群名已替换为示例：「读书会(50)」被读成「读书舍(50)」）。
     所以会话键用归一化 + 相似度比较，不要求逐字相同，
     否则每次误读都会被当成"切了会话"，反复清空状态。

触发规则沿用安卓版（ChatCaptureService.kt:125）：只在最新一条来自对方时才分析；
去重沿用 ChatModels.kt:15-16 的最后 6 条签名。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JEV_DIR = REPO / "tools" / "jev"
SPIKE = REPO / "tools" / "ios-spike"
for p in (str(JEV_DIR), str(SPIKE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pipeline as pl          # noqa: E402

DEFAULT_RELATIONSHIP = (
    "对方是我的伴侣；from=me 的是我发的，from=other 的是对方发的"
)
# 标题相似度下限：低于此值才认为是换了会话（容忍视觉模型读错一两个字）
TITLE_SIMILARITY = 0.60
SIGNATURE_N = 6


def normalize_title(t: str | None) -> str:
    """归一化标题：去空白/括号/成员数，避免「XX(50)」和「XX」被当成两个会话。"""
    if not t:
        return ""
    s = t.strip()
    for ch in "（）()【】[]<>《》 ":
        s = s.replace(ch, "")
    # 去掉末尾的纯数字（群成员数）
    while s and s[-1].isdigit():
        s = s[:-1]
    return s


def same_session(a: str | None, b: str | None) -> bool:
    """两个标题是否算同一个会话（模糊匹配，容忍误读）。"""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= TITLE_SIMILARITY


def signature(msgs: list[dict], n: int = SIGNATURE_N) -> str:
    """消息签名，用于去重。对齐 ChatModels.kt:15-16（取最后 n 条）。"""
    tail = msgs[-n:]
    return "|".join(f"{m.get('side')}:{m.get('text')}" for m in tail)


@dataclass
class Gate:
    """是否该分析，以及为什么。"""

    go: bool
    reason: str
    new_session: bool = False


@dataclass
class Result:
    profile: str = ""
    calibrated: bool = True
    is_group: bool = False
    title: str | None = None
    answers: dict = field(default_factory=dict)
    consistency: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    ranked: list[tuple[str, float]] = field(default_factory=list)
    timings: dict = field(default_factory=dict)


class Engine:
    """持有会话状态（当前会话键、已分析过的签名），负责该不该分析与整条链。"""

    def __init__(self, relationship: str | None = None, whitelist: tuple[str, ...] = (),
                 judge_provider: str | None = None, analysis_provider: str | None = None,
                 vision_provider: str | None = None, timeout: float = 120.0,
                 log=print) -> None:
        self.canon = pl.load_canonical()
        self.relationship = relationship or DEFAULT_RELATIONSHIP
        self.whitelist = tuple(w.strip() for w in whitelist if w.strip())
        self.judge_provider = judge_provider
        self.analysis_provider = analysis_provider
        self.vision_provider = vision_provider
        self.timeout = timeout
        self.log = log

        self.session_title: str | None = None
        self.last_signature: str = ""
        self.analyses = 0

    # ---------------------------------------------------------------- 闸门

    def allowed(self, title: str | None) -> bool:
        """会话白名单：空 = 全部放行；非空 = 标题包含任一项（子串匹配，同安卓版语义）。"""
        if not self.whitelist:
            return True
        if not title:
            return False
        return any(w in title for w in self.whitelist)

    def gate(self, msgs: list[dict], title: str | None) -> Gate:
        """该不该分析：会话切换 → 白名单 → 最新一条来自对方 → 签名未重复。"""
        if not msgs:
            return Gate(False, "没读到消息")

        new_session = bool(self.session_title) and not same_session(self.session_title, title)
        if self.session_title is None:
            self.session_title = title
            new_session = True
        elif new_session:
            self.log(f"会话切换：「{self.session_title}」→「{title}」，清空历史与去重状态")
            self.session_title = title
            self.last_signature = ""

        if not self.allowed(title):
            return Gate(False, f"会话「{title}」不在白名单", new_session)

        latest = (msgs[-1].get("side") or "").strip()
        if latest != "other":
            return Gate(False, "最新一条不是对方发的（你已回复，等对方新消息）", new_session)

        sig = signature(msgs)
        if sig == self.last_signature:
            return Gate(False, "内容与上次分析相同，跳过（省一次调用）", new_session)

        return Gate(True, "对方有新消息", new_session)

    def commit(self, msgs: list[dict]) -> None:
        self.last_signature = signature(msgs)
        self.analyses += 1

    # ---------------------------------------------------------------- 判断

    def judge(self, msgs: list[dict], is_group: bool) -> Result:
        """第一步：判断（约 1 秒）。先把结论画出来，不让人等起草。"""
        pname, profile = pl.pick_profile(self.canon, is_group)
        rel = self.relationship or profile.get("relationship_default") or DEFAULT_RELATIONSHIP
        state = pl.to_python_state(msgs, rel, None, is_group, None)
        questions = profile["judge_questions"]

        answers, jmeta = pl.judge(state, questions, self.timeout, self.judge_provider)
        r = Result(profile=pname, calibrated=bool(profile.get("calibrated", True)),
                   is_group=is_group, title=self.session_title, answers=answers)
        r.timings["judge_s"] = jmeta.get("elapsed_s")
        r.timings["judge_usage"] = jmeta.get("usage")
        r.consistency = self._consistency(answers, pname)
        # 起草与排序要用同一个 state，暂存在实例上避免重复构造
        self._state = state
        self._rel = rel
        return r

    def _consistency(self, answers: dict, pname: str) -> list[str]:
        """跨题一致性：答案互相矛盾时提醒，不直接当结论用。"""
        try:
            import consistency as csx

            pred = {
                k: (v.get("noul") if "noul" in v else v.get("choice") if "choice" in v else v.get("score"))
                for k, v in answers.items()
            }
            rules = csx.one_on_one_rules() if pname == "one_on_one" else csx.group_rules()
            hits = []
            for name, _why, applies, violated in rules:
                try:
                    if applies(pred) and violated(pred):
                        hits.append(name)
                except Exception:
                    continue
            return hits
        except Exception as exc:
            self.log(f"（一致性检查跳过: {exc}）")
            return []

    # ---------------------------------------------------------------- 起草 + 排序

    def draft_and_rank(self, msgs: list[dict], result: Result) -> Result:
        """第二步：起草 3 条候选 + Jev 排序（慢，回来后补进面板）。"""
        state = getattr(self, "_state", None)
        rel = getattr(self, "_rel", self.relationship)
        if state is None:
            return result

        cands, dmeta = pl.draft(self.canon, msgs, rel, self.timeout,
                                self.analysis_provider, None)
        result.candidates = cands
        result.timings["draft_s"] = dmeta.get("elapsed_s")

        scored, rmeta = pl.rank(state, cands, self.timeout, self.judge_provider, result.is_group)
        result.ranked = scored
        result.timings["rank_s"] = rmeta.get("elapsed_s")
        result.timings["rank_picked"] = rmeta.get("picked")
        return result

    # ---------------------------------------------------------------- 展示用

    def permission_hint(self) -> str:
        """缺哪个密钥就说清楚，别让人猜。"""
        import providers as pv

        lines = []
        for role in ("judge", "analysis", "perception"):
            try:
                pid, prov = pv.role_provider(role, {
                    "judge": self.judge_provider,
                    "analysis": self.analysis_provider,
                    "perception": self.vision_provider,
                }[role])
                env = prov.get("api_key_env")
                import os

                ok = bool(os.environ.get(env or ""))
                if not ok and not prov.get("api_key_optional"):
                    lines.append(f"{role} 需要 {env}（provider {pid}）")
            except Exception as exc:
                lines.append(f"{role} 未配置: {exc}")
        return "；".join(lines)
