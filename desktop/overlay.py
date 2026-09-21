"""悬浮窗：半透明置顶面板，展示判断结果与 3 条候选回复，一键复制。

形态沿用安卓版（OverlayController.kt），而不是参考项目那种贴右上角的不透明窗口：
  * 半透明，让底下的聊天透出来（透明度可调）
  * 收起时是一个可拖拽的小气泡，危险等级用颜色点在小球上
  * 展开是从气泡下面长出来的面板，挪到屏幕左上，不挡输入框
  * 两段式渲染：判断（约 1 秒）先画出来，候选（慢）回来再补——不让人干等

阈值与中文映射表逐字搬自 OverlayController.kt:461-486，保持一致。

v1 范围：只有「复制」，没有「填入」。发送永远由人手动点。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import font as tkfont

# ---------------------------------------------------------------- 与安卓版一致的映射

INTENT = {
    "confirm_you_care": "确认你在不在乎",
    "vent_anger": "在发泄情绪",
    "request_action": "要你办事",
    "seek_explanation": "要个解释",
    "casual_chat": "随便聊聊",
    "close_topic": "事情过去了",
}
NEEDS = {
    "apology": "道歉",
    "action": "具体行动",
    "explanation": "解释",
    "care": "你的在乎",
    "nothing": "（不用做什么）",
}
ACTION = {
    "check_history": "翻聊天记录",
    "apologize": "先道歉",
    "give_commitment": "给承诺",
    "explain": "解释清楚",
    "acknowledge": "接住情绪",
    "say_less": "少说两句",
    "make_plan": "定个安排",
}

COLOR_RED = "#DC2626"
COLOR_AMBER = "#D97706"
COLOR_GREEN = "#16A34A"
COLOR_INK = "#111827"
COLOR_SUB = "#6B7280"
COLOR_ACCENT = "#3A7AFE"
COLOR_TOP_CARD = "#EAF1FF"
COLOR_CARD = "#F3F4F6"

PANEL_W = 340
BUBBLE = 52


def danger_color(level: int) -> str:
    if level >= 6:
        return COLOR_RED
    if level >= 3:
        return COLOR_AMBER
    return COLOR_GREEN


def danger_word(level: int) -> str:
    if level >= 8:
        return "很危险"
    if level >= 6:
        return "偏危险"
    if level >= 3:
        return "留神"
    return "安全"


def score_index(ans: dict) -> float:
    """danger_level 的分档加权值：sum(档位 × 概率)，退化用 score。

    与 calibrate.py 的算法一致——验收门禁（MAE<1.0）也是按这个口径量的。
    """
    probs = ans.get("probabilities") or {}
    try:
        if probs:
            return sum(int(k) * float(p) for k, p in probs.items())
    except (TypeError, ValueError):
        pass
    return float(ans.get("score") or 0)


@dataclass
class State:
    """面板当前要显示的东西。判断先到、候选后到，所以分开放。"""

    title: str | None = None
    answers: dict = field(default_factory=dict)
    ranked: list[tuple[str, float]] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)
    status: str = ""
    error: str | None = None
    busy: bool = False
    latency_s: float | None = None
    perceiver: str = ""
    group: bool = False


class Overlay:
    """tkinter 悬浮窗。所有更新都必须走 after() 回到主线程。"""

    def __init__(self, on_reanalyze=None, on_copy=None, on_settings=None, on_toggle=None) -> None:
        self.on_reanalyze = on_reanalyze or (lambda: None)
        self.on_copy = on_copy or (lambda _t: None)
        self.on_settings = on_settings or (lambda: None)
        self.on_toggle = on_toggle or (lambda _on: None)

        self.root = tk.Tk()
        self.root.title("Jev 副驾")
        self.root.overrideredirect(True)                  # 无边框
        self.root.attributes("-topmost", True)            # 置顶
        self.root.attributes("-alpha", 0.93)              # 半透明，让聊天透出来
        self.root.configure(bg="#FFFFFF")

        self.state = State()
        self.expanded = False
        self.show_transcript = False   # 感知原文默认收起
        self._drag = {"x": 0, "y": 0, "moved": False}
        self._body = None

        self._fonts()
        self._build_bubble()
        self._build_panel()
        self._place_collapsed()
        self._bind_drag(self.bubble_wrap)
        self.root.after(200, self._reapply_alpha)

    # ---------------------------------------------------------------- 字体

    def _fonts(self) -> None:
        fam = "Microsoft YaHei UI"
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_body = tkfont.Font(family=fam, size=10)
        self.f_bold = tkfont.Font(family=fam, size=10, weight="bold")
        self.f_head = tkfont.Font(family=fam, size=12, weight="bold")

    # ---------------------------------------------------------------- 结构

    def _build_bubble(self) -> None:
        self.bubble_wrap = tk.Frame(self.root, bg="#FFFFFF", width=BUBBLE, height=BUBBLE)
        self.bubble_wrap.pack_propagate(False)
        self.bubble = tk.Label(self.bubble_wrap, text="Jev", bg=COLOR_ACCENT, fg="#FFFFFF",
                               font=self.f_bold, width=4, height=1)
        self.bubble.pack(fill="both", expand=True)
        self.bubble.bind("<Button-1>", lambda _e: self.toggle())
        self.bubble_wrap.bind("<Button-1>", lambda _e: None)

    def _build_panel(self) -> None:
        self.panel = tk.Frame(self.root, bg="#FFFFFF")
        head = tk.Frame(self.panel, bg="#FFFFFF")
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text="Jev 分析", bg="#FFFFFF", fg=COLOR_INK, font=self.f_head).pack(side="left")
        tk.Label(head, text="⚙", bg="#FFFFFF", fg=COLOR_SUB, font=self.f_body,
                 cursor="hand2").pack(side="right", padx=(6, 0))
        head.winfo_children()[-1].bind("<Button-1>", lambda _e: self.on_settings())
        tk.Label(head, text="✕", bg="#FFFFFF", fg=COLOR_SUB, font=self.f_body,
                 cursor="hand2").pack(side="right")
        head.winfo_children()[-1].bind("<Button-1>", lambda _e: self.toggle())

        self.status = tk.Label(self.panel, text="", bg="#FFFFFF", fg=COLOR_SUB,
                               font=self.f_small, anchor="w", justify="left")
        self.status.pack(fill="x", padx=10)

        # 正文放进可滚动 Canvas：内容长短不定（判断先到、候选后到、转录可长可短），
        # 直接 pack 会把面板撑到占满屏（实测 781px / 屏高 1080）。
        outer = tk.Frame(self.panel, bg="#FFFFFF")
        outer.pack(fill="both", expand=True, padx=(10, 4), pady=(4, 8))
        self.canvas = tk.Canvas(outer, bg="#FFFFFF", highlightthickness=0)
        self.vsb = tk.Scrollbar(outer, command=self.canvas.yview, width=10)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg="#FFFFFF")
        self._body_win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._body_win, width=e.width),
        )
        self.canvas.bind_all("<MouseWheel>",
                             lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    # ---------------------------------------------------------------- 位置与拖拽

    def _place_collapsed(self) -> None:
        self.root.geometry(f"{BUBBLE}x{BUBBLE}+40+180")

    def _bind_drag(self, w) -> None:
        def down(e):
            self._drag.update(x=e.x_root - self.root.winfo_x(),
                              y=e.y_root - self.root.winfo_y(), moved=False)

        def move(e):
            nx, ny = e.x_root - self._drag["x"], e.y_root - self._drag["y"]
            if abs(nx - self.root.winfo_x()) + abs(ny - self.root.winfo_y()) > 4:
                self._drag["moved"] = True
            self.root.geometry(f"+{max(0, nx)}+{max(0, ny)}")

        def up(_e):
            self._drag["moved"] = False

        for ev, fn in (("<ButtonPress-1>", down), ("<B1-Motion>", move), ("<ButtonRelease-1>", up)):
            w.bind(ev, fn, add="+")

    def _fit_panel(self) -> None:
        """面板高度按内容自适应，但封顶在屏高的 66%（超出部分靠正文滚动看）。

        坑：展开时用 winfo_height() 拿到的是布局前的 1，必须 update_idletasks()
        之后再读 reqheight，而且内容每次变化（判断先到、候选后到）都要重新适配。
        """
        if not self.expanded:
            return
        try:
            self.root.update_idletasks()
            h = self.panel.winfo_reqheight()
            cap = int(self.root.winfo_screenheight() * 0.66)
        except Exception:
            return
        self.root.geometry(
            f"{PANEL_W}x{max(80, min(h, cap))}+{self.root.winfo_x()}+{self.root.winfo_y()}"
        )

    def toggle(self) -> None:
        self.expanded = not self.expanded
        if self.expanded:
            self.bubble_wrap.pack_forget()
            self.panel.pack(fill="both", expand=True)
            self.root.geometry(f"{PANEL_W}x100+30+80")
            self.render()
        else:
            self.panel.pack_forget()
            self.bubble_wrap.pack(fill="both", expand=True)
            self._place_collapsed()

    def collapse(self) -> None:
        if self.expanded:
            self.toggle()

    # ---------------------------------------------------------------- 外部 API
    # 下面这些可能从采集/感知线程调用，一律 after() 回主线程

    def _ui(self, fn, *a) -> None:
        try:
            self.root.after(0, lambda: fn(*a))
        except Exception:
            pass

    def set_status(self, text: str) -> None:
        self._ui(self._set_status, text)

    def set_transcript(self, msgs: list[tuple[str, str]]) -> None:
        self._ui(self._set_transcript, msgs)

    def show_judgment(self, answers: dict, latency_s: float | None = None) -> None:
        self._ui(self._show_judgment, answers, latency_s)

    def show_replies(self, ranked: list[tuple[str, float]]) -> None:
        self._ui(self._show_replies, ranked)

    def show_error(self, msg: str) -> None:
        self._ui(self._show_error, msg)

    def show_loading(self, what: str = "分析中…") -> None:
        self._ui(self._show_loading, what)

    def set_alpha(self, percent: int) -> None:
        self._alpha = max(0.35, min(1.0, percent / 100.0))

    def _reapply_alpha(self) -> None:
        try:
            self.root.attributes("-alpha", getattr(self, "_alpha", 0.93))
        except Exception:
            pass

    # ---------------------------------------------------------------- 内部

    def _set_status(self, text: str) -> None:
        self.state.status = text
        self.status.configure(text=text)

    def _set_transcript(self, msgs: list[tuple[str, str]]) -> None:
        self.state.transcript = msgs
        self.render()

    def _show_judgment(self, answers: dict, latency_s: float | None) -> None:
        self.state.answers = answers
        self.state.latency_s = latency_s
        self.state.error = None
        self.state.busy = False
        if not self.expanded:
            self.toggle()
        self.render()

    def _show_replies(self, ranked: list[tuple[str, float]]) -> None:
        self.state.ranked = ranked
        self.render()

    def _show_error(self, msg: str) -> None:
        self.state.error = msg
        self.state.busy = False
        if not self.expanded:
            self.toggle()
        self.render()

    def _show_loading(self, what: str) -> None:
        self.state.busy = True
        self.state.error = None
        self._set_status(what)
        if not self.expanded:
            self.toggle()
        self.render()

    # ---------------------------------------------------------------- 渲染

    def clear(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def render(self) -> None:
        self.clear()
        s = self.state

        if s.error:
            tk.Label(self.body, text="出错了", bg="#FFFFFF", fg=COLOR_RED,
                     font=self.f_bold, anchor="w").pack(fill="x")
            tk.Label(self.body, text=s.error, bg="#FFFFFF", fg=COLOR_SUB, font=self.f_small,
                     wraplength=PANEL_W - 30, justify="left", anchor="w").pack(fill="x")
            self._reanalyze_btn()
            return

        a = s.answers
        if a:
            dl = a.get("danger_level") or {}
            lvl = round(score_index(dl)) if dl else None
            if lvl is not None:
                self._danger_badge(lvl)

            ti = a.get("true_intent") or {}
            if ti.get("choice"):
                tk.Label(self.body, text=f"对方真实意图：{INTENT.get(ti['choice'], ti['choice'])}",
                         bg="#FFFFFF", fg=COLOR_INK, font=self.f_bold,
                         anchor="w").pack(fill="x", pady=(2, 0))
                conf = ti.get("confidence")
                if isinstance(conf, (int, float)):
                    tk.Label(self.body, text=f"把握 {conf * 100:.0f}%", bg="#FFFFFF",
                             fg=COLOR_SUB, font=self.f_small, anchor="w").pack(fill="x")

            bits = []
            sn = a.get("she_needs") or {}
            if sn.get("choice"):
                bits.append("要" + NEEDS.get(sn["choice"], sn["choice"]))
            ba = a.get("best_action") or {}
            if ba.get("choice"):
                bits.append(ACTION.get(ba["choice"], ba["choice"]))
            srn = a.get("should_reply_now")
            if isinstance(srn, dict):
                srn = srn.get("noul")
            if isinstance(srn, (int, float)):
                bits.append("可给实质" if srn >= 0.5 else "先别给实质")
            if bits:
                tk.Label(self.body, text="  ·  ".join(bits), bg="#FFFFFF", fg="#374151",
                         font=self.f_body, anchor="w", justify="left",
                         wraplength=PANEL_W - 30).pack(fill="x", pady=(2, 0))

            tr = a.get("tension_resolved")
            if isinstance(tr, dict):
                tr = tr.get("noul")
            if isinstance(tr, (int, float)) and tr >= 0.7:
                tk.Label(self.body, text="✓ 紧张已缓解", bg="#FFFFFF", fg=COLOR_GREEN,
                         font=self.f_small, anchor="w").pack(fill="x")

        self._divider()
        tk.Label(self.body, text="候选回复（Jev 排序）", bg="#FFFFFF", fg="#9CA3AF",
                 font=self.f_small, anchor="w").pack(fill="x")
        if s.ranked:
            for i, (text, p) in enumerate(s.ranked, 1):
                self._reply_card(i, text, p)
        elif s.busy:
            tk.Label(self.body, text="生成中…", bg="#FFFFFF", fg=COLOR_SUB,
                     font=self.f_small, anchor="w").pack(fill="x")
        else:
            tk.Label(self.body, text="（还没有候选）", bg="#FFFFFF", fg=COLOR_SUB,
                     font=self.f_small, anchor="w").pack(fill="x")

        if s.transcript:
            self._divider()
            n = len(s.transcript)
            arrow = "▾" if self.show_transcript else "▸"
            lbl = tk.Label(self.body, text=f"{arrow} 感知层读到的原文（{n} 条，核对误读）",
                           bg="#FFFFFF", fg="#9CA3AF", font=self.f_small,
                           anchor="w", cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda _e: self._toggle_transcript())
            # 默认收起：转录是排查误读用的，不该占主视野。
            # 展开时用固定高度的 Text（可滚动），避免长转录把面板撑到占满屏。
            if self.show_transcript:
                box = tk.Frame(self.body, bg="#FFFFFF")
                box.pack(fill="x")
                txt = tk.Text(box, height=6, wrap="word", bg="#F9FAFB", fg=COLOR_SUB,
                              font=self.f_small, relief="flat", highlightthickness=0,
                              padx=6, pady=4)
                sb = tk.Scrollbar(box, command=txt.yview, width=10)
                txt.configure(yscrollcommand=sb.set)
                sb.pack(side="right", fill="y")
                txt.pack(side="left", fill="both", expand=True)
                txt.insert("1.0", "\n".join(
                    f"{'我' if side == 'me' else '对方'}：{t}" for side, t in s.transcript[-12:]
                ))
                txt.configure(state="disabled")

        if s.latency_s:
            tk.Label(self.body, text=f"感知耗时 {s.latency_s:.1f}s", bg="#FFFFFF",
                     fg="#9CA3AF", font=self.f_small, anchor="w").pack(fill="x", pady=(4, 0))
        self._reanalyze_btn()
        self._fit_panel()

    def _toggle_transcript(self) -> None:
        self.show_transcript = not self.show_transcript
        self.render()

    def _danger_badge(self, level: int) -> None:
        color = danger_color(level)
        row = tk.Frame(self.body, bg="#FFFFFF")
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text=f"危险 {level}/9", bg=color, fg="#FFFFFF", font=self.f_bold,
                 padx=8, pady=2).pack(side="left")
        tk.Label(row, text=f"  {danger_word(level)}", bg="#FFFFFF", fg=color,
                 font=self.f_bold).pack(side="left")

    def _reply_card(self, rank: int, text: str, prob: float) -> None:
        bg = COLOR_TOP_CARD if rank == 1 else COLOR_CARD
        card = tk.Frame(self.body, bg=bg)
        card.pack(fill="x", pady=3)
        tk.Label(card, text=f"#{rank} · {prob * 100:.0f}%", bg=bg, fg=COLOR_ACCENT,
                 font=self.f_small, anchor="w").pack(fill="x", padx=8, pady=(5, 0))
        tk.Label(card, text=text, bg=bg, fg=COLOR_INK, font=self.f_body, anchor="w",
                 justify="left", wraplength=PANEL_W - 50).pack(fill="x", padx=8)
        btns = tk.Frame(card, bg=bg)
        btns.pack(fill="x", padx=8, pady=(3, 6))
        tk.Label(btns, text="复制", bg=COLOR_ACCENT, fg="#FFFFFF", font=self.f_small,
                 padx=12, pady=3, cursor="hand2").pack(side="left")
        btns.winfo_children()[-1].bind("<Button-1>", lambda _e, t=text: self.on_copy(t))

    def _divider(self) -> None:
        # 不能用安卓版的 8 位 ARGB（#1F000000），tkinter 只认 6 位 RGB
        tk.Frame(self.body, bg="#E5E7EB", height=1).pack(fill="x", pady=5)

    def _reanalyze_btn(self) -> None:
        lbl = tk.Label(self.body, text="重新分析", bg="#FFFFFF", fg=COLOR_SUB,
                       font=self.f_small, cursor="hand2")
        lbl.pack(pady=(6, 0))
        lbl.bind("<Button-1>", lambda _e: self.on_reanalyze())

    def run(self) -> None:
        self.root.mainloop()
