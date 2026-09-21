"""截图 → 消息列表（谁说的 + 说了什么）。

这是 iOS 版采集层的算法原型：安卓版靠无障碍节点树拿到「气泡文本 + 屏幕坐标」
（ChatAppAdapter.kt:46-60），iOS 没有这个能力，只能靠 OCR 拿到「文本 + 文字框坐标」。
本文件把后者变成与安卓采集层同构的输出，供 tools/jev 的判断层直接消费。

归属判定给了三种规则，用 --rule 切换，好让实测数据来决定用哪个：
  center  中心法：文字框中心 x > 屏宽/2 → 我发的（直接沿用 ChatAppAdapter.kt:60 的思路）
  edge    边缘法：文字框右缘贴右边 → 我发的；左缘贴左边 → 对方发的（长消息更稳）
  color   颜色法：看文字框周围的底色，微信自己的气泡是绿色 #95EC69，对方是白色
           （截图场景独有，安卓版拿不到像素才只能用几何）

用法:
  python extract.py --image shot.png                    # 打印消息列表
  python extract.py --image shot.png --rule edge        # 换归属规则
  python extract.py --image shot.png --debug out.png    # 画归属标注图，人工核对
  python extract.py --image shot.png --json out.json    # 输出 JSON 给 pipeline.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 中文 Windows 控制台默认 CP936，中文输出会乱码。
# 用 reconfigure 而不是 TextIOWrapper：后者被多个模块重复包会关闭底层 buffer。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 与安卓对齐的常量

# 消息区上下边界。安卓飞书适配器用屏高 14%~84%（ChatAppAdapter.kt:103-104）。
# 顶部这 14% 是"标题栏下沿"的**上界**，安卓真正用的是 min(首个气泡 top, 14%)
# （ChatAppAdapter.kt:68）——不能当成固定裁剪线用，否则没有时间戳的会话首条消息会被删。
# 这里 0.09 是"状态栏 + 导航栏"的实际高度上界，仅在没有识别出标题时兜底。
BAND_TOP = 0.09
BAND_TOP_FALLBACK_ANDROID = 0.14
BAND_BOTTOM = 0.84

# 贴边判定阈值（占屏宽比例）。微信气泡左右各有约 12% 的边距（头像 + 内边距）。
EDGE_RIGHT = 0.80
EDGE_LEFT = 0.20

# 时间戳/系统提示，正则取自 ChatAppAdapter.kt:25-28
TS_PATTERNS = [
    re.compile(r"^\d{1,2}[:：]\d{2}([:：]\d{2})?$"),
    re.compile(r"^\d{1,2}月\d{1,2}日"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"^(昨天|今天|前天|明天|上午|下午|晚上|凌晨)"),
    re.compile(r"^(星期|周)[一二三四五六日天]"),
]

# 微信自身气泡绿 #95EC69 的判据（宽松些，容忍 JPEG 压缩与半透明叠加）
def _is_wechat_green(r: int, g: int, b: int) -> bool:
    return g > 190 and (g - r) > 25 and (g - b) > 55 and r < 210


def _is_bubble_white(r: int, g: int, b: int) -> bool:
    return r > 244 and g > 244 and b > 244


# ---------------------------------------------------------------- 数据结构


@dataclass
class TextBox:
    """一块 OCR 文本及其屏幕坐标（像素，左上原点）。"""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class Msg:
    """一条消息。side 与安卓 ChatModels.kt:3-4 的取值一致（me / other）。"""

    side: str
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    conf: float = 1.0
    rule: str = ""

    def as_tuple(self) -> tuple[str, str]:
        return (self.side, self.text)


@dataclass
class Snapshot:
    title: str | None
    messages: list[Msg] = field(default_factory=list)
    width: int = 0
    height: int = 0
    rule: str = ""

    def signature(self, n: int = 6) -> str:
        """消息签名，用于去重。对齐安卓 ChatModels.kt:15-16（取最后 n 条）。"""
        return "|".join(f"{m.side}:{m.text}" for m in self.messages[-n:])

    def to_json(self) -> dict:
        return {
            "title": self.title,
            "width": self.width,
            "height": self.height,
            "rule": self.rule,
            "messages": [
                {"side": m.side, "text": m.text, "conf": round(m.conf, 3)} for m in self.messages
            ],
        }


# ---------------------------------------------------------------- OCR


_READER = None


def get_reader(langs: list[str] | None = None):
    """惰性加载 easyocr（首次运行会下载模型）。"""
    global _READER
    if _READER is None:
        import easyocr

        _READER = easyocr.Reader(langs or ["ch_sim", "en"], gpu=False, verbose=False)
    return _READER


def ocr_boxes(image_path: str | Path, langs: list[str] | None = None) -> list[TextBox]:
    """对整图做 OCR，返回文字块。"""
    from PIL import Image

    reader = get_reader(langs)
    res = reader.readtext(str(image_path))
    boxes: list[TextBox] = []
    for poly, text, conf in res:
        xs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        text = text.strip()
        if not text:
            continue
        boxes.append(TextBox(min(xs), min(ys), max(xs), max(ys), text, float(conf)))
    _ = Image  # 保持 import 意图明确
    return boxes


# ---------------------------------------------------------------- 过滤


def is_timestampish(text: str) -> bool:
    return any(p.match(text) for p in TS_PATTERNS)


def in_message_band(b: TextBox, height: float) -> bool:
    top = height * BAND_TOP
    bottom = height * BAND_BOTTOM
    return b.cy >= top and b.cy <= bottom


# 输入栏特征词。只有底部真的存在输入栏时才裁掉底部区域——否则会把最新的消息删掉
# （微信桌面版截图、以及任何没截到输入框的图，最新消息恰好就在最下面）。
INPUT_BAR_HINTS = (
    "发送",
    "说点什么",
    "输入",
    "按住 说话",
    "按住说话",
    "send",
    "type a message",
    "message",
)


def has_input_bar(boxes: list[TextBox], height: float) -> bool:
    """底部区域里有没有输入栏的文字。检出才裁底，避免误删最新消息。"""
    bottom = height * BAND_BOTTOM
    for b in boxes:
        if b.cy >= bottom:
            t = b.text.strip().lower()
            if any(h in t for h in INPUT_BAR_HINTS):
                return True
    return False


def filter_noise(
    boxes: list[TextBox],
    height: float,
    title_box: TextBox | None = None,
    input_bar: bool | None = None,
) -> tuple[list[TextBox], list[TextBox]]:
    """去掉时间戳/系统提示/输入框区。返回 (保留, 被丢弃)。

    消息区上边界取 min(固定百分比, 标题栏下沿)——这是安卓 ChatAppAdapter.kt:68
    「min(首个气泡 top, 屏高 14%)」的等价做法。只用固定百分比会把**没有时间戳**的
    会话首条消息当成标题栏删掉（合成用例 c06 实测到的 bug）。

    下边界只在检测到输入栏时才生效。写死 84% 会把最新的消息删掉——
    真实桌面截图实测：最后两条消息（含 Jev 要判断的那条）整条丢失。
    """
    top = height * BAND_TOP
    if title_box is not None:
        top = min(top, title_box.y1 + max(8.0, title_box.h * 0.35))
    if input_bar is None:
        input_bar = has_input_bar(boxes, height)
    bottom = height * BAND_BOTTOM if input_bar else height
    kept: list[TextBox] = []
    dropped: list[TextBox] = []
    for b in boxes:
        # 用中心点比较（不是 y0）：首条消息可能紧贴导航栏，整体高度会跨过边界
        if b.cy < top or b.cy > bottom:
            dropped.append(b)
            continue
        if is_timestampish(b.text):
            dropped.append(b)
            continue
        kept.append(b)
    return kept, dropped


# ---------------------------------------------------------------- 归属


def side_by_center(b: TextBox | Line, width: float) -> str:
    """中心法：沿用安卓 ChatAppAdapter.kt:60 的 cx > width/2。

    注意：安卓拿的是**气泡节点**的 bounds，所以中心法安全；
    这里拿到的是**文字行**的框，长消息折行会越过中线，中心法会判错（实测 86.2%）。
    """
    return "me" if b.cx > width / 2 else "other"


def side_by_edge(b: TextBox | Line, width: float) -> str:
    """边缘法：先看有没有贴边，贴不上再退回中心法。"""
    if b.x1 >= width * EDGE_RIGHT:
        return "me"
    if b.x0 <= width * EDGE_LEFT:
        return "other"
    return side_by_center(b, width)


def _ring_votes(im, b: TextBox | Line, pad: int) -> tuple[int, int]:
    """在文字框外圈 pad 像素处采样，数绿色/白色像素。"""
    W, H = im.size
    x0 = max(0, int(b.x0) - pad)
    x1 = min(W - 1, int(b.x1) + pad)
    y0 = max(0, int(b.y0) - pad)
    y1 = min(H - 1, int(b.y1) + pad)
    if x1 <= x0 or y1 <= y0:
        return (0, 0)
    green = white = 0
    for x in range(x0, x1 + 1, 2):
        for y in (y0, y1):
            r, g, bl = im.getpixel((x, y))
            if _is_wechat_green(r, g, bl):
                green += 1
            elif _is_bubble_white(r, g, bl):
                white += 1
    for y in range(y0, y1 + 1, 2):
        for x in (x0, x1):
            r, g, bl = im.getpixel((x, y))
            if _is_wechat_green(r, g, bl):
                green += 1
            elif _is_bubble_white(r, g, bl):
                white += 1
    return (green, white)


def side_by_color(image_path: str | Path, b: TextBox | Line, width: float) -> str:
    """颜色法：看文字行周围的气泡底色——微信自己的气泡是绿 #95EC69，对方是白。

    为什么这条规则在 OCR 场景下比几何法更对：它读的是**气泡本身**，
    而文字行在气泡里的位置是任意的（短行贴边、长行折行），几何位置不可靠。
    先按 0.3 倍行高采样（气泡内边距通常大于这个值），不明确就放大到 0.6 倍再试。
    """
    from PIL import Image

    global _PIXELS
    im = _PIXELS.get(str(image_path))
    if im is None:
        im = Image.open(image_path).convert("RGB")
        _PIXELS[str(image_path)] = im

    for ratio in (0.30, 0.60):
        pad = max(3, int(b.h * ratio))
        green, white = _ring_votes(im, b, pad)
        if green + white > 0 and green != white:
            return "me" if green > white else "other"
    return side_by_edge(b, width)


_PIXELS: dict[str, object] = {}


RULES = {"center": side_by_center, "edge": side_by_edge, "color": side_by_color}


def assign_sides(
    boxes: list[TextBox] | list[Line],
    width: float,
    rule: str,
    image_path: str | Path | None = None,
) -> list[tuple[object, str]]:
    out: list[tuple[object, str]] = []
    for b in boxes:
        if rule == "color" and image_path is not None:
            s = side_by_color(image_path, b, width)
        else:
            s = RULES[rule](b, width)
        out.append((b, s))
    return out


# ---------------------------------------------------------------- 同行合并
#
# 必要性：OCR 会按标点/字距把同一行文字切成多块（"回，我周六下午到家。" →
# "回，" + "我周六下午到家。"）。不先合并，一行就会被当成多条消息。
# 合并后每一行才是「一行文字」，也才是归属判定的最小单位。


@dataclass
class Line:
    """合并后的一行文字。"""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float
    n_boxes: int = 1

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def _v_overlap(a: TextBox | Line, b: TextBox | Line) -> float:
    return min(a.y1, b.y1) - max(a.y0, b.y0)


def group_lines(boxes: list[TextBox]) -> list[Line]:
    """把 OCR 文字块按视觉行合并。同行的判据是垂直重叠超过较矮者的 50%。"""
    ordered = sorted(boxes, key=lambda b: (b.cy, b.x0))
    groups: list[list[TextBox]] = []
    for b in ordered:
        target = None
        # 只比最近的几组：列表已按 cy 排序，同一行的块必然相邻
        for g in reversed(groups[-3:]):
            gx0 = min(x.x0 for x in g)
            gx1 = max(x.x1 for x in g)
            gy0 = min(x.y0 for x in g)
            gy1 = max(x.y1 for x in g)
            gline = Line(gx0, gy0, gx1, gy1, "", 1.0)
            if _v_overlap(gline, b) > 0.5 * min(gline.h, b.h):
                target = g
                break
        if target is None:
            groups.append([b])
        else:
            target.append(b)

    lines: list[Line] = []
    for g in groups:
        g.sort(key=lambda b: b.x0)
        text = g[0].text
        prev = g[0]
        for b in g[1:]:
            gap = b.x0 - prev.x1
            # 间距大到半个字高以上就补一个空格（英文/链接需要），中文正常粘连
            text += (" " if gap > 0.4 * b.h else "") + b.text
            prev = b
        lines.append(
            Line(
                x0=min(b.x0 for b in g),
                y0=min(b.y0 for b in g),
                x1=max(b.x1 for b in g),
                y1=max(b.y1 for b in g),
                text=text,
                conf=min(b.conf for b in g),
                n_boxes=len(g),
            )
        )
    lines.sort(key=lambda ln: (ln.y0, ln.x0))
    return lines


# ---------------------------------------------------------------- 折行合并成消息


def _same_bubble(prev: Line, cur: Line) -> bool:
    """判断 cur 是不是 prev 同一气泡里的下一行。

    用的是「垂直间隙小 + 水平范围重叠」。
    注意间隙允许为负：OCR 相邻两行的文字框会互相重叠几个像素（字体框比行距高），
    实测 c02/c05 就是因此没合上——原来写死 gap < -2 即拒绝，折行永远合并不了。
    只拒绝明显往回跳的情况。
    已知局限：同一人连发两条短消息时，间隙与折行接近，会误合并（见 ocr_spike.md）。
    彻底解决要靠气泡级分割，而不是文字行几何。
    """
    gap = cur.y0 - prev.y1
    if gap < -prev.h * 0.5:
        return False
    if gap > prev.h * 0.75:
        return False
    inter = min(prev.x1, cur.x1) - max(prev.x0, cur.x0)
    narrower = min(prev.w, cur.w)
    if narrower <= 0:
        return False
    return inter / narrower > 0.5


def merge_messages(pairs: list[tuple[Line, str]], rule: str) -> list[Msg]:
    """把同一气泡内的多行合并成一条消息，按纵向顺序排列。"""
    ordered = sorted(pairs, key=lambda p: (p[0].y0, p[0].x0))
    msgs: list[Msg] = []
    prev: Line | None = None
    for ln, side in ordered:
        if prev is not None and msgs and msgs[-1].side == side and _same_bubble(prev, ln):
            m = msgs[-1]
            m.text += ln.text
            m.x0 = min(m.x0, ln.x0)
            m.x1 = max(m.x1, ln.x1)
            m.y1 = max(m.y1, ln.y1)
            m.conf = min(m.conf, ln.conf)
        else:
            msgs.append(
                Msg(
                    side=side,
                    text=ln.text,
                    x0=ln.x0,
                    x1=ln.x1,
                    y0=ln.y0,
                    y1=ln.y1,
                    conf=ln.conf,
                    rule=rule,
                )
            )
        prev = ln
    return msgs


# ---------------------------------------------------------------- 标题


def guess_title_box(boxes: list[TextBox], width: float, height: float) -> TextBox | None:
    """聊天标题的那块文本：顶部条里居中的最宽文本。

    安卓版同样靠几何推断标题栏（ChatAppAdapter.kt:78-80）。
    返回 box 而不只是文本，因为下沿要用来当消息区上边界。

    原先还要求宽度占屏宽 10%，结果两个字的名字（如"妈妈"）识别不到标题，
    进而退回固定顶带、把首条消息删掉——合成用例 c04 实测踩到。
    改成：居中判据收紧到屏宽 10%，宽度只留绝对下限。
    """
    top = height * BAND_TOP
    cands = [
        b
        for b in boxes
        if b.cy < top
        and abs(b.cx - width / 2) < width * 0.10
        and b.w >= 40
        and not is_timestampish(b.text)
    ]
    if not cands:
        return None
    cands.sort(key=lambda b: -b.w)
    return cands[0]


def guess_title(boxes: list[TextBox], width: float, height: float) -> str | None:
    b = guess_title_box(boxes, width, height)
    return b.text if b is not None else None


# ---------------------------------------------------------------- 主流程


def extract(
    image_path: str | Path,
    rule: str = "edge",
    boxes: list[TextBox] | None = None,
    langs: list[str] | None = None,
) -> Snapshot:
    """截图 → Snapshot（标题 + 消息列表）。boxes 可注入，便于合成图复用同一逻辑。"""
    from PIL import Image

    image_path = str(image_path)
    with Image.open(image_path) as im:
        W, H = im.size

    if boxes is None:
        boxes = ocr_boxes(image_path, langs)

    title_box = guess_title_box(boxes, W, H)
    title = title_box.text if title_box is not None else None
    kept, _dropped = filter_noise(boxes, H, title_box)
    lines = group_lines(kept)
    pairs = assign_sides(lines, W, rule, image_path)
    msgs = merge_messages(pairs, rule)  # type: ignore[arg-type]
    return Snapshot(title=title, messages=msgs, width=W, height=H, rule=rule)


def draw_debug(image_path: str | Path, snap: Snapshot, out_path: str | Path) -> None:
    """把归属结果画到图上，人工核对用。蓝=对方(other)，红=我(me)。"""
    from PIL import Image, ImageDraw

    im = Image.open(image_path).convert("RGB")
    d = ImageDraw.Draw(im)
    W, H = im.size
    d.line([(W / 2, H * BAND_TOP), (W / 2, H * BAND_BOTTOM)], fill=(255, 0, 255), width=3)
    d.line([(0, H * BAND_TOP), (W, H * BAND_TOP)], fill=(255, 0, 255), width=3)
    d.line([(0, H * BAND_BOTTOM), (W, H * BAND_BOTTOM)], fill=(255, 0, 255), width=3)
    for m in snap.messages:
        color = (220, 20, 20) if m.side == "me" else (20, 90, 220)
        d.rectangle([m.x0 - 4, m.y0 - 4, m.x1 + 4, m.y1 + 4], outline=color, width=4)
        d.text((m.x0, max(0, m.y0 - 26)), f"{m.side}", fill=color)
    im.save(out_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="截图 → 消息列表（谁说的 + 说了什么）")
    ap.add_argument("--image", required=True, help="聊天截图路径")
    ap.add_argument("--rule", default="edge", choices=sorted(RULES), help="归属判定规则")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    ap.add_argument("--debug", help="把归属标注图画到该路径")
    ap.add_argument("--langs", default="ch_sim,en", help="OCR 语言，逗号分隔")
    args = ap.parse_args()

    langs = [s.strip() for s in args.langs.split(",") if s.strip()]
    snap = extract(args.image, rule=args.rule, langs=langs)

    print(f"图: {args.image}  {snap.width}x{snap.height}  规则: {args.rule}")
    print(f"标题: {snap.title!r}")
    print(f"消息数: {len(snap.messages)}")
    print("-" * 60)
    for i, m in enumerate(snap.messages, 1):
        tag = "我" if m.side == "me" else "对方"
        print(f"{i:>2}. [{tag}] ({m.conf:.2f}) {m.text}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(snap.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON 已写 {args.json_out}")
    if args.debug:
        draw_debug(args.image, snap, args.debug)
        print(f"标注图已写 {args.debug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
