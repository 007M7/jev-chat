"""生成带 ground truth 的合成微信一对一聊天截图。

为什么要合成：真实的微信截图只有用户手上的那几张，且无法穷举边界情况。
合成图能确定性地量出「归属规则」和「折行合并」的准确率——把 OCR 质量这个变量隔离开，
因为合成图上的文字是清晰的、OCR 几乎不出错，测出来的错就是算法错。

产出：<out>/synth_XX.png 与 <out>/synth_XX.json（ground truth）。

用法:
  python synth.py --out out/synth            # 生成默认 6 张
  python synth.py --out out/synth --count 12
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# ---------------------------------------------------------------- 版式常量（对齐真实微信截图）

W, H = 1200, 2670
FONT_SIZE = 34
LINE_H = 46
PAD_X = 16  # 气泡内左右内边距
PAD_Y = 12  # 气泡内上下内边距
AVATAR = 96
SIDE_MARGIN = 20
AVATAR_GAP = 16
MAX_BUBBLE_W = 720  # 气泡最大宽度（约屏宽 60%）

BAND_TOP = 190  # 导航栏下沿
BAND_BOTTOM = 2330  # 输入框上沿

BG = (243, 244, 246)
BUBBLE_ME = (149, 236, 105)  # 微信自身气泡绿 #95EC69
BUBBLE_OTHER = (255, 255, 255)
INK = (26, 26, 26)
GREY_INK = (150, 150, 150)
SEND_GREEN = (7, 193, 96)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def load_font(size: int = FONT_SIZE):
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    raise SystemExit("找不到可用的中文字体，请检查 " + ", ".join(FONT_CANDIDATES))


@dataclass
class GtMsg:
    side: str
    text: str
    x0: int
    y0: int
    x1: int
    y1: int


def wrap(text: str, font, max_w: int) -> list[str]:
    """按像素宽度贪心折行（中文按字断行）。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if font.getlength(trial) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def rrect(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def draw_tail(d, side: str, box, fill):
    """气泡小尖角。颜色法只看底色，尖角是为了让合成图更接近真实截图。"""
    x0, y0, x1, y1 = box
    if side == "me":
        d.polygon([(x1 - 2, y0 + 18), (x1 + 12, y0 + 26), (x1 - 2, y0 + 34)], fill=fill)
    else:
        d.polygon([(x0 + 2, y0 + 18), (x0 - 12, y0 + 26), (x0 + 2, y0 + 34)], fill=fill)


def render(messages: list[tuple[str, str]], title: str, show_ts: bool = True) -> tuple[Image.Image, list[GtMsg]]:
    """把 (side, text) 列表画成微信风格的截图，返回图与 ground truth。"""
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    font = load_font()
    f_title = load_font(40)
    f_small = load_font(28)

    # 顶部状态栏
    d.rectangle([0, 0, W, 70], fill=(240, 240, 240))
    d.text((30, 22), "18:59", font=f_small, fill=INK)
    d.text((W - 190, 22), "5G  98", font=f_small, fill=INK)
    # 导航栏
    d.rectangle([0, 70, W, BAND_TOP], fill=(237, 237, 237))
    d.text((40, 100), "<", font=load_font(50), fill=INK)
    tw = f_title.getlength(title)
    d.text(((W - tw) / 2, 108), title, font=f_title, fill=INK)

    y = BAND_TOP + 40
    gt: list[GtMsg] = []
    if show_ts:
        ts = "昨天 21:03"
        d.text(((W - f_small.getlength(ts)) / 2, y), ts, font=f_small, fill=GREY_INK)
        y += 70

    for side, text in messages:
        max_text_w = MAX_BUBBLE_W - 2 * PAD_X
        lines = wrap(text, font, max_text_w)
        text_w = max(font.getlength(ln) for ln in lines)
        bubble_w = int(text_w) + 2 * PAD_X
        bubble_h = LINE_H * len(lines) + 2 * PAD_Y

        if side == "me":
            bx1 = W - SIDE_MARGIN - AVATAR - AVATAR_GAP
            bx0 = bx1 - bubble_w
            fill = BUBBLE_ME
        else:
            bx0 = SIDE_MARGIN + AVATAR + AVATAR_GAP
            bx1 = bx0 + bubble_w
            fill = BUBBLE_OTHER

        box = (bx0, y, bx1, y + bubble_h)
        rrect(d, box, 14, fill)
        draw_tail(d, side, box, fill)
        # 头像
        if side == "me":
            d.rounded_rectangle([W - SIDE_MARGIN - AVATAR, y, W - SIDE_MARGIN, y + AVATAR], 12,
                                fill=(120, 160, 220))
        else:
            d.rounded_rectangle([SIDE_MARGIN, y, SIDE_MARGIN + AVATAR, y + AVATAR], 12,
                                fill=(200, 170, 140))

        ty = y + PAD_Y
        for ln in lines:
            d.text((bx0 + PAD_X, ty), ln, font=font, fill=INK)
            ty += LINE_H

        gt.append(GtMsg(side=side, text=text, x0=bx0, y0=y, x1=bx1, y1=y + bubble_h))
        y += bubble_h + 34
        if y > BAND_BOTTOM - 200:
            break

    # 底部输入栏
    d.rectangle([0, BAND_BOTTOM, W, H], fill=(246, 246, 246))
    d.rounded_rectangle([30, BAND_BOTTOM + 30, W - 190, BAND_BOTTOM + 120], 12, fill=(255, 255, 255))
    d.text((60, BAND_BOTTOM + 55), "说点什么", font=font, fill=(170, 170, 170))
    d.rounded_rectangle([W - 170, BAND_BOTTOM + 30, W - 30, BAND_BOTTOM + 120], 10, fill=SEND_GREEN)
    d.text((W - 140, BAND_BOTTOM + 55), "发送", font=font, fill=(255, 255, 255))
    return im, gt


# ---------------------------------------------------------------- 用例集

CASES: list[tuple[str, str, list[tuple[str, str]], bool]] = [
    (
        "c01_short_mixed",
        "小美",
        [
            ("other", "在吗"),
            ("me", "在的"),
            ("other", "晚饭吃了吗"),
            ("me", "还没呢"),
        ],
        True,
    ),
    (
        "c02_long_wrapped",
        "小美",
        [
            ("other", "你今天是不是又忘了我跟你说过什么？我早上出门前特意提醒了你两遍，你还是没当回事"),
            ("me", "我记得，你先别提示我，让我自己说，我肯定能想起来"),
            ("other", "那你说"),
        ],
        True,
    ),
    (
        "c03_consecutive_same_side",
        "阿伟",
        [
            ("other", "在忙吗"),
            ("other", "有个事想问你"),
            ("me", "不忙你说"),
            ("me", "什么事"),
            ("other", "算了"),
        ],
        True,
    ),
    (
        "c04_punct_heavy",
        "妈妈",
        [
            ("other", "周末回来吃饭吗？"),
            ("me", "回，我周六下午到家。"),
            ("other", "好，那我提前准备。你想吃什么？"),
            ("me", "随便，你做的都好吃"),
        ],
        True,
    ),
    (
        "c05_very_long_one_side",
        "小美",
        [
            ("other", "我跟你说我们今天开会开到六点半，然后领导又临时加了个需求，说要明天早上交，我都快疯了，你说这合理吗"),
            ("me", "确实过分了"),
            ("other", "对吧！你也这么觉得吧！"),
        ],
        True,
    ),
    (
        "c06_no_timestamp",
        "同事老张",
        [
            ("other", "方案我看过了"),
            ("me", "有问题吗"),
            ("other", "第三章的数据需要再核一下，其他地方没问题"),
            ("me", "好，我今晚改完发你"),
        ],
        False,
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成带 ground truth 的合成微信截图")
    ap.add_argument("--out", default="out/synth", help="输出目录前缀")
    ap.add_argument("--count", type=int, default=0, help="只生成前 N 张")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = CASES[: args.count] if args.count else CASES

    manifest = []
    for name, title, msgs, show_ts in cases:
        im, gt = render(msgs, title, show_ts)
        png = out / f"{name}.png"
        im.save(png)
        payload = {
            "name": name,
            "title": title,
            "image": str(png).replace("\\", "/"),
            "width": W,
            "height": H,
            "messages": [asdict(g) for g in gt],
        }
        (out / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest.append(payload)
        print(f"生成 {png}  消息 {len(gt)} 条")

    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n共 {len(manifest)} 张，manifest 已写 {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
