"""窗口采集循环：WGC 取帧 → 切消息区 → 像素门 → 静默门 → 交给下游。

为什么是这个结构（每一步都有实测依据）：
  * 用 WGC 而不是 BitBlt/ImageGrab：实测微信窗口被其它窗口完全盖住时，
    WGC 仍能拿到真实内容（遮挡色占比 0.000），而屏幕像素类方案会失效。
  * 像素门放在**本地**：云端感知一次约 5 秒且要花钱，绝不能每帧都调。
    只有消息区像素真的变了才往下走。这是整个成本控制的关键闸门。
  * 比较范围只取消息区，不取整窗：输入框里光标闪烁会让整窗每帧都在变。
  * 静默门：消息区连续 quiet 秒没变才算"稳定"，避免把滚动中的中间态送出去；
    另有 max_wait 上限，避免循环动画（比如会动的表情包）永远等不到静默。
  * 最小化时 WGC 零帧——不做"强制还原"（那是改动别人的窗口），
    只上报状态让上层提示用户。
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# 依赖装在仓库内的 vendor（C 盘只剩 2.6 GB，不往系统 site-packages 装）
_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import area as area_mod

WECHAT_CLASSES = ("Qt51514QWindowIcon", "WeChatMainWndForPC", "ChatWnd")

# 静默门：连续多久没变算稳定；以及最长等待上限
SETTLE_QUIET_S = 0.30
SETTLE_MAX_S = 1.20
# 像素门采样间隔
POLL_S = 0.10


def find_wechat_window() -> dict | None:
    """定位微信主窗口。只读窗口元信息，不碰目标进程内存。

    两个坑（都真踩过）：
      * 微信有多个 class 命中的窗口，还有个 176x199 的隐藏辅助窗口（标题 'Weixin'）。
        按"面积最大"选会选错——主窗口最小化时矩形会塌缩成 160x28，
        反而比辅助窗口小。所以：先排除不可见窗口，再按**还原尺寸**评分。
      * 最小化时 GetWindowRect 无意义，要用 GetWindowPlacement 的 rcNormalPosition。
    """
    import win32api
    import win32gui
    import win32process

    found: list[dict] = []

    def cb(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        if cls not in WECHAT_CLASSES:
            return True
        # 隐藏窗口直接排除：那个 'Weixin' 辅助窗口就是 vis=0
        if not win32gui.IsWindowVisible(hwnd):
            return True
        exe = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            try:
                exe = Path(win32process.GetModuleFileNameEx(h, 0)).name
            finally:
                win32api.CloseHandle(h)
        except Exception:
            pass
        if exe and "weixin" not in exe.lower() and "wechat" not in exe.lower():
            return True
        try:
            normal = win32gui.GetWindowPlacement(hwnd)[4]   # rcNormalPosition（还原后的矩形）
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        nw, nh = normal[2] - normal[0], normal[3] - normal[1]
        found.append({
            "hwnd": hwnd, "class": cls, "exe": exe,
            "title": win32gui.GetWindowText(hwnd),
            "rect": rect,
            "size": (rect[2] - rect[0], rect[3] - rect[1]),
            "normal_size": (nw, nh),
            "visible": True,
            "minimized": bool(win32gui.IsIconic(hwnd)),
        })
        return True

    win32gui.EnumWindows(cb, None)
    if not found:
        return None
    # 主窗口 = 还原尺寸最大的那个
    best = sorted(found, key=lambda w: w["normal_size"][0] * w["normal_size"][1], reverse=True)[0]
    if best["normal_size"][0] * best["normal_size"][1] < 100_000:
        return None
    return best


def window_state(hwnd: int) -> dict:
    """窗口当前状态，供上层决定"暂停并提示"还是继续。"""
    import win32gui

    return {
        "minimized": bool(win32gui.IsIconic(hwnd)),
        "visible": bool(win32gui.IsWindowVisible(hwnd)),
        "exists": bool(win32gui.IsWindow(hwnd)),
    }


def frame_usable(rgb) -> tuple[bool, str]:
    """帧健康检查：WGC 刚启动时会给一帧未渲染的空帧，不能让它的像素污染定位。

    实测教训：采集启动后的第一帧是空帧，此时"右半区众数色"被算成黑色，
    定位退化成一整屏 (0,32,1920,1032)，而且会被缓存下来一直用错。
    """
    import numpy as np

    a = rgb.astype(np.int16)
    if (a.max(axis=2) < 12).mean() > 0.90:
        return False, "全黑帧（窗口未渲染或采集刚启动）"
    if float(a.std()) < 1.5:
        return False, "近似纯色帧（无内容）"
    return True, ""


def area_plausible(a: area_mod.Area) -> tuple[bool, str]:
    """定位合理性校验：退化结果必须被拒，否则下游会拿到整屏而不是消息区。"""
    if a.width <= 0 or a.height <= 0:
        return False, "帧尺寸异常"
    if a.left <= 0:
        return False, "没找到会话列表右沿（左边界为 0）"
    if a.bottom >= a.height:
        return False, "没找到输入区分隔线（下边界=窗高）"
    if a.right - a.left < 200 or a.bottom - a.top < 200:
        return False, "消息区过小"
    return True, ""


@dataclass
class Frame:
    """一张稳定的消息区截图 + 它的坐标上下文。"""

    rgb: object                     # (H,W,3) uint8 RGB
    area: area_mod.Area
    frame_size: tuple[int, int]     # 整窗客户区尺寸
    at: float = field(default_factory=time.time)


class CaptureLoop:
    """WGC 采集 + 两道本地闸门。稳定帧通过 on_settled 回调送出。

    on_settled(frame: Frame) 会在 WGC 的采集线程里被调用——
    里面**不能**做耗时的事（OCR/网络），只能塞队列。
    """

    def __init__(self, on_settled, log=print) -> None:
        self.on_settled = on_settled
        self.log = log
        self._control = None
        self._cap = None
        self._last = None           # 上一次比对的像素（消息区）
        self._last_change_at = 0.0
        self._pending_since = 0.0
        self._last_area: area_mod.Area | None = None
        self._area_checked_at = 0.0
        self._area_every = 1.0      # 定位多久重算一次（窗口可能被移动/缩放）
        self._lock = threading.Lock()
        self._flushing = False
        self._flusher: threading.Thread | None = None
        self.frames_seen = 0
        self.frames_emitted = 0
        self.frames_skipped = 0
        self.last_error: str | None = None
        self.last_skip_reason: str | None = None
        self.last_area_error: str | None = None
        self.hwnd: int | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture

        self.hwnd = hwnd
        cap = WindowsCapture(window_hwnd=hwnd, draw_border=False)

        @cap.event
        def on_frame_arrived(frame, capture_control):  # noqa: ANN001
            try:
                self._on_frame(frame)
            except Exception as exc:  # 回调里抛异常会让整条采集线程死掉
                self.last_error = f"{type(exc).__name__}: {exc}"

        @cap.event
        def on_closed():
            self.log("采集会话关闭（窗口被关闭？）")

        self._cap = cap
        self._control = cap.start_free_threaded()

        # WGC 只在内容变化时送帧，所以"最后一帧到达"不等于"已经静默"——
        # 静默必须由独立定时器判定，否则内容稳定后不再送帧、稳定帧永远发不出去。
        self._flushing = True
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()
        self.log(f"采集已启动 hwnd={hwnd}")

    def stop(self) -> None:
        self._flushing = False
        if self._control is not None:
            try:
                self._control.stop()
                self._control.wait()
            except Exception:
                pass
            self._control = None

    def _flush_loop(self) -> None:
        """独立定时器：内容停止变化 SETTLE_QUIET_S 秒后把稳定帧送出去。"""
        while self._flushing:
            time.sleep(POLL_S)
            now = time.time()
            with self._lock:
                if not (self._pending_since and self._last is not None):
                    continue
                if now - self._last_change_at < SETTLE_QUIET_S:
                    continue
                crop, a = self._last, self._last_area
                self._pending_since = 0.0
            if a is not None:
                self._emit(crop, a, now)

    @property
    def running(self) -> bool:
        return self._control is not None and not self._control.is_finished()

    # ------------------------------------------------------------ 帧处理

    def _on_frame(self, frame) -> None:
        import numpy as np

        self.frames_seen += 1
        # 缓冲区回调后就失效，必须立刻拷成自己的数组
        bgra = np.ascontiguousarray(frame.frame_buffer)
        rgb = bgra[:, :, :3][:, :, ::-1].copy()
        now = time.time()

        # 帧健康检查：空帧/黑帧直接丢，不进像素门也不进定位
        ok, why = frame_usable(rgb)
        if not ok:
            self.frames_skipped += 1
            self.last_skip_reason = why
            return

        # 消息区定位偶尔重算：窗口可能被拖动/缩放/换主题。
        # 无效定位**不缓存**——缓存了就会一直用错（第一帧空帧的坑）。
        if self._last_area is None or now - self._area_checked_at > self._area_every:
            self._area_checked_at = now
            try:
                cand = area_mod.detect_area(rgb)
                good, why = area_plausible(cand)
                if good:
                    self._last_area = cand
                    self.last_area_error = None
                else:
                    self.last_area_error = why
            except Exception as exc:
                self.last_area_error = f"定位失败: {exc}"

        a = self._last_area
        if a is None:
            return
        crop = rgb[a.top : a.bottom, a.left : a.right]

        # --- 像素门：消息区没变就到此为止（这一步是免费的，也是花钱与否的分界）
        if self._last is not None and crop.shape == self._last.shape:
            if np.array_equal(crop, self._last):
                return   # 没变：静默计时不动，交给 flusher 判静默

        with self._lock:
            if self._last is None or self._pending_since == 0.0:
                self._pending_since = now
            self._last = crop
            self._last_area = a
            self._last_change_at = now
            # max_wait 上限：循环动画（会动的表情包）永远等不到静默，强制送一次
            force = now - self._pending_since >= SETTLE_MAX_S
            if force:
                self._pending_since = 0.0
        if force:
            self._emit(crop, a, now)

    def _emit(self, crop, a: area_mod.Area, now: float) -> None:
        self.frames_emitted += 1
        self._pending_since = 0.0
        self.on_settled(Frame(rgb=crop, area=a,
                              frame_size=(a.width, a.height), at=now))
