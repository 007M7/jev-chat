"""消息区定位：从整窗截图里切出「正在显示的对话」那一块。

所有阈值都来自本机真实测得的像素统计（见 docs/win/gate0_findings.md），
但代码里不写死坐标——窗口移动/缩放/换深浅色都重新算，避免"微信改一次布局就失效"。

实测（微信 4.1.13.65 / 深色模式 / 窗口 1936x1048 / 客户区 1920x1032）：
  聊天区底色 (30,30,31)  左边界 x=289（会话列表右沿）  上边界 y=32（顶部空条下沿）
  下边界 y=815 —— 注意：输入区底色与消息区**完全相同**，唯一区分依据是那条分隔线，
  它整行同色 (39,39,40) 占 0.979。所以分隔线判据必须容忍 0.98 以下的占比，
  阈值卡到 0.985 会跳过它、最后把窗口底边当成下边界（这个 bug 真踩过）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 整行/整列「同色」的容忍度：同一色系内允许 ±tol 的通道差
COLOR_TOL = 4
# 分隔线判据：整行同色占比下限（实测那条线 0.979）+ 与聊天区底色的最小差异
SEP_MIN_FRAC = 0.95
SEP_MIN_DIFF = 4
# 顶部空条判据：整行同色占比高于此值即视为"空条"（实测=1.000）
CHROME_MIN_FRAC = 0.90
# 左边界判据：该列聊天区底色占比高于此值即视为"已在消息区"（实测 x=289 处 0.964）
PANE_MIN_FRAC = 0.50
# 分隔线只在窗高这个比例以下找，避免把消息行误判
SEP_SEARCH_FROM = 0.35


@dataclass
class Area:
    """消息区矩形（客户区坐标，左上原点）。"""

    left: int
    top: int
    right: int
    bottom: int
    pane_bg: tuple[int, int, int]
    width: int
    height: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    @property
    def size(self) -> tuple[int, int]:
        return (self.right - self.left, self.bottom - self.top)

    def crop(self, frame):
        """从整窗帧里切出消息区。"""
        return frame[self.top : self.bottom, self.left : self.right]

    def as_dict(self) -> dict:
        return {"rect": self.rect, "size": self.size, "pane_bg": list(self.pane_bg)}


def _col_frac(mat, color, tol: int = COLOR_TOL):
    """沿最后一维求「等于指定颜色」的比例。

    传 (N,3) 得标量；传 (W,H,3)（转置过的图）得长度 W 的数组——逐列占比。
    """
    import numpy as np

    d = np.abs(mat.astype(np.int16) - np.array(color, np.int16)).max(axis=-1)
    return (d <= tol).mean(axis=-1)


def pane_bg_color(a) -> tuple[int, int, int]:
    """聊天区底色 = 右半区出现最多的颜色。右半区一定是消息区，不会取到会话列表。"""
    import numpy as np

    h, w, _ = a.shape
    right = a[:, w // 2 :, :].reshape(-1, 3)
    cols, counts = np.unique(right, axis=0, return_counts=True)
    return tuple(int(x) for x in cols[counts.argmax()])


def detect_area(a) -> Area:
    """定位消息区。a 是 (H,W,3) 的 RGB 数组（整窗客户区）。"""
    import numpy as np

    h, w, _ = a.shape
    bg = pane_bg_color(a)

    # 左边界：逐列算聊天区底色占比，取跃升到 >PANE_MIN_FRAC 的第一列
    col_frac = _col_frac(np.transpose(a, (1, 0, 2)), bg)
    left = 0
    for x in range(w):
        if col_frac[x] > PANE_MIN_FRAC:
            left = x
            break

    # 上边界：从 y=0 起第一条"整行不再同色"的行（顶部空条的下沿）
    corner = tuple(int(v) for v in a[0, 0])
    top = 0
    for y in range(h):
        if _col_frac(a[y], corner) < CHROME_MIN_FRAC:
            top = y
            break

    # 下边界：消息区里第一条"整行同色且不是聊天区底色"的横向分隔线 = 输入区上沿
    bottom = h
    for y in range(int(h * SEP_SEARCH_FROM), h):
        seg = a[y, left:, :]
        cols, counts = np.unique(seg, axis=0, return_counts=True)
        ci = counts.argmax()
        modal = cols[ci]
        if counts[ci] / len(seg) > SEP_MIN_FRAC and int(np.abs(modal.astype(np.int16) - np.array(bg, np.int16)).max()) > SEP_MIN_DIFF:
            bottom = y
            break

    return Area(left=left, top=top, right=w, bottom=bottom, pane_bg=bg, width=w, height=h)
