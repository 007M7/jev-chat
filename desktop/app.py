"""Jev 副驾（Windows）：读屏 → 判断 → 候选 → 悬浮窗 → 一键复制。

只读：只截图读屏，不 hook、不注入、不碰微信进程内存、不读它的数据库。
不发送：v1 只做到「复制」，「填入输入框」都不做，更不会点发送。

跑起来：
  set DEEPSEEK_API_KEY=...        # 感知 + 起草
  set JEV_API_KEY=...             # 判断 + 排序（TypeSafe 直连）
  python desktop/app.py
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import capture as cap          # noqa: E402
import engine as eng           # noqa: E402
import overlay as ov           # noqa: E402
import perceive as pc          # noqa: E402


class App:
    """把采集线程、感知线程、UI 串起来。UI 更新一律经 overlay 的 after() 回主线程。"""

    def __init__(self, args) -> None:
        self.args = args
        self.work: queue.Queue = queue.Queue(maxsize=1)
        self.busy = False
        self.last_frame: cap.Frame | None = None
        self.stopping = False

        self.engine = eng.Engine(
            relationship=args.relationship,
            whitelist=tuple(args.whitelist.split(",")) if args.whitelist else (),
            judge_provider=args.judge_provider,
            analysis_provider=args.analysis_provider,
            vision_provider=args.vision_provider,
            timeout=args.timeout,
            log=self._log,
        )
        self.overlay = ov.Overlay(
            on_reanalyze=self.reanalyze,
            on_copy=self.copy,
            on_settings=lambda: self.overlay.set_status("设置页见 docs/win/ 说明（v1 用命令行参数）"),
        )
        self.overlay.set_alpha(args.opacity)
        self.capture: cap.CaptureLoop | None = None

    # ---------------------------------------------------------------- 日志

    def _log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    # ---------------------------------------------------------------- 回调

    def copy(self, text: str) -> None:
        try:
            self.overlay.root.clipboard_clear()
            self.overlay.root.clipboard_append(text)
            self.overlay.set_status("已复制到剪贴板，粘贴后自己发送")
        except Exception as exc:
            self._log(f"复制失败: {exc}")

    def reanalyze(self) -> None:
        """手动重跑：绕过签名去重。"""
        if self.last_frame is None:
            self.overlay.set_status("还没采到帧，等一下")
            return
        self._log("手动重新分析")
        self.overlay.show_loading("重新分析中…")
        self._submit(self.last_frame, force=True)

    def on_settled(self, frame: cap.Frame) -> None:
        """采集线程回调——只做入队，绝不做耗时的事。"""
        self.last_frame = frame
        self._submit(frame, force=False)

    def _submit(self, frame: cap.Frame, force: bool) -> None:
        try:
            while True:                      # 只保留最新一帧
                self.work.get_nowait()
        except queue.Empty:
            pass
        try:
            self.work.put_nowait((frame, force))
        except queue.Full:
            pass

    # ---------------------------------------------------------------- 工作线程

    def worker(self) -> None:
        while not self.stopping:
            try:
                frame, force = self.work.get(timeout=0.3)
            except queue.Empty:
                self._poll_window_state()
                continue
            if self.busy:
                continue
            self.busy = True
            try:
                self._analyze(frame, force)
            except Exception as exc:
                self._log(f"分析异常: {type(exc).__name__}: {exc}")
                self.overlay.show_error(f"{type(exc).__name__}: {exc}")
            finally:
                self.busy = False

    def _poll_window_state(self) -> None:
        """空闲时检查窗口状态：最小化时 WGC 不出帧，要主动提示而不是静默装死。"""
        w = cap.find_wechat_window()
        if w is None:
            self.overlay.set_status("没找到微信窗口")
        elif w["minimized"]:
            self.overlay.set_status("微信已最小化 —— 还原窗口才能读屏（不自动动你的窗口）")

    def _analyze(self, frame: cap.Frame, force: bool) -> None:
        t0 = time.time()
        self.overlay.set_status("读屏中…")

        msgs, structured, meta = pc.perceive(
            frame.rgb, provider=self.args.vision_provider, timeout=self.args.timeout
        )
        title = structured.get("title")
        senders = structured.get("senders")
        perceiver_s = meta.get("elapsed_s")
        self._log(f"感知 {perceiver_s}s  标题={title!r}  群聊={structured.get('is_group')}  {len(msgs)} 条")

        gate = self.engine.gate(msgs, title)
        if not gate.go and not force:
            self.overlay.set_status(f"{gate.reason}（感知 {perceiver_s}s）")
            self.overlay.set_transcript([(m["side"], m["text"]) for m in msgs])
            return

        is_group = bool(structured.get("is_group"))
        self.overlay.set_transcript([(m["side"], m["text"]) for m in msgs])
        self.overlay.show_loading("Jev 判断中…")

        # 两段式：判断先出来
        try:
            result = self.engine.judge(msgs, is_group)
        except Exception as exc:
            self._log(f"判断失败: {exc}")
            self.overlay.show_error(f"判断失败：{exc}")
            return
        self.engine.commit(msgs)
        self.overlay.show_judgment(result.answers, perceiver_s)
        if result.consistency:
            self._log(f"⚠ 跨题矛盾: {result.consistency}")
        if not result.calibrated:
            self._log(f"⚠ 档案 {result.profile} 尚未校准，结论仅供参考")

        # 候选慢慢补
        self.overlay.set_status("起草候选…")
        try:
            result = self.engine.draft_and_rank(msgs, result)
        except Exception as exc:
            self._log(f"起草/排序失败: {exc}")
            self.overlay.set_status(f"候选失败：{exc}")
            return
        self.overlay.show_replies(result.ranked)
        total = time.time() - t0
        self._log(f"完成 总耗时 {total:.1f}s  档案={result.profile}  "
                  f"判断={result.timings.get('judge_s')}s 起草={result.timings.get('draft_s')}s "
                  f"排序={result.timings.get('rank_s')}s")
        self.overlay.set_status(f"就绪（总 {total:.1f}s）")

    # ---------------------------------------------------------------- 启动

    def run(self) -> int:
        hint = self.engine.permission_hint()
        if hint:
            self._log(f"缺少密钥：{hint}")
            self.overlay.show_error(f"缺少密钥：{hint}")
        else:
            self._log("密钥齐备")

        w = cap.find_wechat_window()
        if w is None:
            self._log("没找到微信窗口，先打开微信")
            self.overlay.show_error("没找到微信窗口。先打开微信再重启本程序。")
        else:
            self._log(f"微信窗口 hwnd={w['hwnd']} size={w['size']} minimized={w['minimized']}")
            if w["minimized"]:
                self.overlay.set_status("微信已最小化 —— 还原窗口才能开始读屏")
            self.capture = cap.CaptureLoop(self.on_settled, log=self._log)
            self.capture.start(w["hwnd"])

        threading.Thread(target=self.worker, daemon=True).start()
        try:
            self.overlay.run()
        finally:
            self.stopping = True
            if self.capture:
                self.capture.stop()
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Jev 副驾（Windows，只读 + 复制）")
    ap.add_argument("--relationship", default=None, help="关系描述（默认取题目档案里的）")
    ap.add_argument("--whitelist", default="", help="会话白名单，逗号分隔；空=全部")
    ap.add_argument("--opacity", type=int, default=93, help="悬浮窗不透明度 35-100")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--vision-provider", default=None, help="覆盖 roles.perception")
    ap.add_argument("--judge-provider", default=None, help="覆盖 roles.judge")
    ap.add_argument("--analysis-provider", default=None, help="覆盖 roles.analysis")
    args = ap.parse_args()
    return App(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
