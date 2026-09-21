"""消息区定位：从整窗截图里切出「正在显示的对话」那一块。纯向量化，单帧约 100ms。

阈值全部来自本机真实像素统计（见 docs/win/gate0_findings.md），但代码里不写死坐标。

## 两个窗口尺寸的实测对照（同一个微信，深色模式）

| | 最大化 1920x1032 | 缩小 972x810 |
|---|---|---|
| 最左图标条底色 | `(45,45,46)` | **`(31,31,32)`** |
| 会话列表底色 | `(47,47,48)` | `(47,47,48)` |
| 聊天区底色 | `(30,30,31)` | `(30,30,31)` |
| 列表/聊天区分界 | x=289 | x=290 |
| 输入区上沿分隔线 | y=815，占比 0.979 | y=592，占比 0.947 |

## 三个踩过的坑

1. **左边界不能用"第一列像聊天区底色"**。缩小后图标条 `(31,31,32)` 与聊天区 `(30,30,31)`
   **只差 1**，逐列算底色占比会在 x=2 就命中，把整个左栏当成聊天区。
   可靠判据是**会话列表的 `(47,47,48)`**（与聊天区差 18），所以先找列表色带、取其右沿，
   再往右走到第一个聊天区底色的列。
2. **下边界的整行同色占比阈值不能卡太紧**。缩小后那条线占比只有 0.947，
   阈值写 0.95 会跳过它、一路扫到窗口底边。另外要防"整行被一条满宽气泡占据"被误判成
   分隔线，所以要求它上下各 3 行都是聊天区底色——**分隔线是细线，气泡不是**。
3. **不要逐列/逐行调 `np.unique`**。初版那么写，1920 宽窗口单帧要 1832ms；
   而它每秒都要重算一次，会把采集回调线程卡住。改成整帧算一次掩码、再用
   `mean(axis=...)` 一次得到逐列/逐行占比，同一帧降到约 100ms。

## 上边界为什么取 0

WGC 给的是客户区，不含系统标题栏。客户区顶部是微信自己的顶栏（会话标题、标签图标），
**而会话标题正是会话隔离需要的**（感知 prompt 本来就要求返回 title，并明确不要把它当消息）。
所以不裁顶部——少一个易碎的启发式，多一个有用信号。
"""

from __future__ import annotations

from dataclasses import dataclass

# 判定"某色属于某带"的容忍度
COLOR_TOL = 4
# 聊天区底色在某一列/行里占比高于此值，即认为该列/行"是聊天区"
PANE_MIN_FRAC = 0.50
# 会话列表色带：某列里该色占比下限（实测 0.73）
BAND_MIN_FRAC = 0.30
# 会话列表色带：与聊天区底色的最小差异（实测差 18，取 6 以防浅色主题下差异变小）
BAND_MIN_DIFF = 6
# 会话列表色带只在这段宽度内找（列表永远在左边）
BAND_SCAN_FRAC = 0.40
# 分隔线：该行聊天区底色占比上限（实测该行只有 0.047）
SEP_PANE_MAX_FRAC = 0.50
# 分隔线：这段"非聊天区底色"的连续行数上限——分隔线是细线（实测 2 行），气泡不是
SEP_MAX_THICKNESS = 4
# 分隔线上下这几行必须是聊天区底色
SEP_CONTEXT_ROWS = 3
SEP_CONTEXT_MIN_FRAC = 0.80
# 分隔线只在窗高这个比例以下找
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
    how: str = ""      # 定位依据，排查用

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
        return {"rect": self.rect, "size": self.size, "pane_bg": list(self.pane_bg),
                "how": self.how}


def _mask_of(a, color, tol: int = COLOR_TOL):
    """整帧里"等于某颜色（容差 tol）"的布尔掩码。算一次，后面全靠它。"""
    import numpy as np

    return np.abs(a.astype(np.int16) - np.array(color, np.int16)).max(axis=2) <= tol


def _dominant_color(a) -> tuple[int, int, int]:
    """出现最多的颜色。对降采样样本做直方图——背景是大面积平色，降采样够用且快得多。"""
    import numpy as np

    sample = a[::3, ::3].reshape(-1, 3)
    if len(sample) == 0:
        return (0, 0, 0)
    cols, counts = np.unique(sample, axis=0, return_counts=True)
    return tuple(int(x) for x in cols[counts.argmax()])


def _diff(c1, c2) -> int:
    return max(abs(int(c1[i]) - int(c2[i])) for i in range(3))


def pane_bg_color(a) -> tuple[int, int, int]:
    """聊天区底色 = 右半区出现最多的颜色。右半区一定是消息区，不会取到会话列表。"""
    _, w, _ = a.shape
    return _dominant_color(a[:, w // 2 :, :])


def _list_color(a, pane_bg) -> tuple[int, int, int] | None:
    """会话列表底色：左段里出现最多、且与聊天区底色明显不同的颜色。"""
    import numpy as np

    _, w, _ = a.shape
    seg = a[:, : max(1, int(w * BAND_SCAN_FRAC)), :]
    sample = seg[::3, ::3].reshape(-1, 3)
    if len(sample) == 0:
        return None
    cols, counts = np.unique(sample, axis=0, return_counts=True)
    for i in np.argsort(-counts)[:8]:         # 只在前几个高频色里挑
        c = tuple(int(x) for x in cols[i])
        if _diff(c, pane_bg) > BAND_MIN_DIFF:
            return c
    return None


def _find_pane_left(a, pane_bg) -> tuple[int, str]:
    """聊天区左边界：先找会话列表色带的右沿，再往右走到第一个聊天区底色的列。"""
    import numpy as np

    _, w, _ = a.shape
    lc = _list_color(a, pane_bg)
    if lc is None:
        return 0, "没找到会话列表色带（列表可能被隐藏）"

    list_col = _mask_of(a, lc).mean(axis=0)        # 逐列：列表色占比
    pane_col = _mask_of(a, pane_bg).mean(axis=0)   # 逐列：聊天区底色占比
    scan_to = max(1, int(w * BAND_SCAN_FRAC))
    idx = np.where(list_col[:scan_to] > BAND_MIN_FRAC)[0]
    if len(idx) == 0:
        return 0, f"列表色 {lc} 没有成带的列"
    list_right = int(idx.max())
    for x in range(list_right + 1, w):
        if pane_col[x] > PANE_MIN_FRAC:
            return x, f"列表({lc})右沿 x={list_right} → 聊天区起始 x={x}"
    return list_right + 1, f"列表右沿 x={list_right}（右侧没找到纯聊天区列）"


def _find_input_top(a, pane_bg, left: int) -> tuple[int, str]:
    """输入区上沿：聊天区里第一条"细的"横向分隔线。

    判据（全部基于逐行聊天区底色占比，**不用极差**——实测缩小窗口里每一行极差都是 91，
    因为聊天区右侧有亮像素，极差完全分不开分隔线和普通消息行）：
      1. 该行不是聊天区底色（实测分隔线 0.047，上下邻行 0.99）
      2. 这段"非聊天区底色"的连续行数很薄（实测 2 行；气泡是几十行）
      3. 紧邻的上下文行是聊天区底色
    """
    import numpy as np

    h, w, _ = a.shape
    if left >= w - 8:
        return h, f"左边界异常 x={left}/{w}，跳过输入区定位"

    pane_row = _mask_of(a[:, left:, :], pane_bg).mean(axis=1)
    not_pane = pane_row < SEP_PANE_MAX_FRAC
    start = int(h * SEP_SEARCH_FROM)

    y = start
    while y < h:
        if not not_pane[y]:
            y += 1
            continue
        e = y
        while e + 1 < h and not_pane[e + 1]:
            e += 1
        thick = e - y + 1
        if thick <= SEP_MAX_THICKNESS:
            lo, hi = y - SEP_CONTEXT_ROWS, e + SEP_CONTEXT_ROWS
            if lo >= 0 and hi < h and pane_row[lo] >= SEP_CONTEXT_MIN_FRAC \
                    and pane_row[hi] >= SEP_CONTEXT_MIN_FRAC:
                return y, f"分隔线 y={y}..{e}（厚 {thick} 行，上下文是聊天区底色）"
        y = e + 1
    return h, "没找到输入区分隔线"


def detect_area(a) -> Area:
    """定位消息区。a 是 (H,W,3) 的 RGB 数组（整窗客户区）。"""
    h, w, _ = a.shape
    bg = pane_bg_color(a)
    left, how_l = _find_pane_left(a, bg)
    bottom, how_b = _find_input_top(a, bg, left)
    return Area(left=left, top=0, right=w, bottom=bottom, pane_bg=bg,
                width=w, height=h, how=f"{how_l}；{how_b}")
