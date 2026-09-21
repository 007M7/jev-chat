"""探针 R2：从整窗截图里定位「消息区」矩形（像素锚点，不硬编码坐标）。

为什么需要它：WGC 给的是整个客户区（实测 1920x1032），里面同时有左侧会话列表、
顶部标题条、底部输入区。感知层只能吃消息区那一块——多喂了输入框草稿/标题/
会话列表，会把没发出去的字和别处的文字当成聊天内容。

做法（全部基于像素统计，窗口移动/缩放/深浅色都跟着走，不写死坐标）：
  1. 聊天区底色 = 右半区出现最多的颜色（实测深色模式 (30,30,31)，占 76%）
  2. 左边界 = 从左往右扫，聊天区底色占比跃升到 >50% 的那一列（实测 x=289）
  3. 上边界 = 顶部那条整行同色的空条的下沿（实测 y=32）
  4. 下边界 = 聊天区里第一条「整行同色、且颜色不是聊天区底色」的横向分隔（实测 y≈815）

实测数据见 docs/win/gate0_findings.md；参考项目用同类思路但它的 README 自认
「输入框拉高超过面板一半会认错消息区」，所以本探针把下边界单独做成可核对的一项。

用法:
  python probe_area.py --image out/capture_4718974.png            # 打印检测出的矩形
  python probe_area.py --image out/capture_4718974.png --debug    # 出调试图核对
  python probe_area.py --live 3                                   # 现场采一帧再检测
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPIKE = Path(__file__).resolve().parent
REPO = SPIKE.parent.parent
VENDOR = REPO / "desktop" / "vendor"
OUT = SPIKE / "out"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


# ---------------------------------------------------------------- 定位


def _pane_bg(a) -> tuple[int, int, int]:
    """聊天区底色：右半区出现最多的颜色。右半区一定是消息区，不会取到会话列表。"""
    import numpy as np

    h, w, _ = a.shape
    right = a[:, w // 2 :, :].reshape(-1, 3)
    cols, counts = np.unique(right, axis=0, return_counts=True)
    return tuple(int(x) for x in cols[counts.argmax()])


def _frac_row(row, color, tol: int = 4):
    """一行里等于指定颜色的像素占比。"""
    import numpy as np

    d = np.abs(row.astype(np.int16) - np.array(color, np.int16)).max(axis=-1)
    return (d <= tol).mean(axis=-1)


def detect_area(a) -> dict:
    """返回消息区矩形与各锚点的实测依据。"""
    import numpy as np

    h, w, _ = a.shape
    bg = _pane_bg(a)

    # 2) 左边界：逐列算聊天区底色占比，取跃升到 >0.5 的第一列
    col_frac = _frac_row(np.transpose(a, (1, 0, 2)), bg)  # 每"列"当成一行来算
    x0 = None
    for x in range(w):
        if col_frac[x] > 0.5:
            x0 = x
            break
    if x0 is None:
        x0 = 0
    # 往左回溯一点：跃变前一列可能已在消息区内（边框像素），从 x0 起保留
    left = x0

    # 3) 上边界：从 y=0 起，找第一条"整行不是单色"的行
    top = 0
    for y in range(h):
        if _frac_row(a[y], tuple(int(v) for v in a[0, 0])) < 0.9:
            top = y
            break

    # 4) 下边界：在消息区横向范围内，找「整行同色且颜色 != 聊天区底色」的分隔行
    #    实测教训：输入区的底色和消息区**完全相同**（都是 (30,30,31)），
    #    唯一能区分两者的就是 y≈815 那条分隔线，所以这条判据是唯一可靠的。
    #    实测那条线众数占比 0.979 —— 阈值不能卡到 0.985，否则会一路跳过它、
    #    最后把窗口底边当成下边界（这个 bug 真踩过）。
    sep_rows = []
    for y in range(int(h * 0.35), h):
        seg = a[y, left:, :]
        cols, counts = np.unique(seg, axis=0, return_counts=True)
        ci = counts.argmax()
        modal = tuple(int(v) for v in cols[ci])
        frac = counts[ci] / len(seg)
        if frac > 0.95 and max(abs(modal[i] - bg[i]) for i in range(3)) > 4:
            sep_rows.append((y, modal, round(float(frac), 3)))

    # 第一条分隔行 = 输入区上沿
    bottom = sep_rows[0][0] if sep_rows else h

    # 第一条分隔行 = 输入区上沿
    bottom = sep_rows[0][0] if sep_rows else h

    return {
        "size": (w, h),
        "pane_bg": bg,
        "left": left,
        "top": top,
        "bottom": bottom,
        "separators": sep_rows[:6],
        "rect": (left, top, w, bottom),
    }


def detect_and_report(a, debug_path: Path | None = None) -> dict:
    info = detect_area(a)
    w, h = info["size"]
    l, t, r, b = info["rect"]
    print(f"图 {w}x{h}   聊天区底色 {info['pane_bg']}")
    print(f"锚点: 左={info['left']}  上={info['top']}  下={info['bottom']}")
    print(f"消息区矩形 = ({l}, {t}) - ({r}, {b})   尺寸 {r-l}x{b-t}  "
          f"占全图 {(r-l)*(b-t)/(w*h):.1%}")
    print(f"下边界候选分隔行: {info['separators']}")

    if debug_path is not None:
        from PIL import Image, ImageDraw

        im = Image.fromarray(a).convert("RGB")
        d = ImageDraw.Draw(im)
        d.rectangle([l, t, r - 1, b - 1], outline=(0, 255, 0), width=4)
        d.line([(l, t), (r, t)], fill=(255, 0, 255), width=2)
        for y, *_rest in info["separators"]:
            d.line([(l, y), (r, y)], fill=(255, 128, 0), width=2)
        im.save(debug_path)
        print(f"调试图: {debug_path}")
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="定位微信窗口的消息区矩形（探针 R2）")
    ap.add_argument("--image", help="用已有截图检测")
    ap.add_argument("--live", type=float, metavar="秒", help="现场采一帧再检测")
    ap.add_argument("--debug", action="store_true", help="输出调试图到 out/")
    args = ap.parse_args()

    import numpy as np
    from PIL import Image

    if args.image:
        a = np.asarray(Image.open(args.image).convert("RGB"))
    elif args.live:
        sys.path.insert(0, str(SPIKE))
        from probe_capture import capture_window, pick_wechat

        w = pick_wechat()
        if w is None or w["minimized"]:
            print("微信没找到或处于最小化，先还原窗口。")
            return 1
        res = capture_window(w["hwnd"], args.live, save=True, save_name="_live_area.png")
        if not res["saved"]:
            print(f"没采到帧: {res['errors']}")
            return 1
        a = np.asarray(Image.open(res["saved"]).convert("RGB"))
    else:
        print("要 --image 或 --live")
        return 2

    dbg = None
    if args.debug:
        OUT.mkdir(parents=True, exist_ok=True)
        dbg = OUT / "area_debug.png"
    detect_and_report(a, dbg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
