#!/usr/bin/env python3
"""把仓库里出现的真实昵称/群名换成伪名。

为什么需要它：这个仓库是**公开**的，而文档与示例数据里带着真实聊天记录里的
群名和人名（视觉模型的实测样例、别名归并的测试序列、UI 截图里抄下来的文案）。
这些不该出现在公开仓库里。

为什么写成脚本而不是手改：替换必须**全库一致**——只改一半会让示例自相矛盾
（比如别名归并的例子只剩一边），而且下次还会漏。跑一次脚本，剩下的都是伪名。

用法：
  python tools/redact_names.py            # 只报告，不改
  python tools/redact_names.py --apply    # 真的改
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 真名 → 伪名。**长的放前面**（"恸." 要在 "恸" 之前替换）。
# 伪名刻意保持原名的形状，示例的技术含义不变：
#   - 视觉模型误读的例子（Akihero/Astero）仍然只差一个字母
#   - 「昵称带点」的例子（恸.）仍然带点
#   - 群名误读的例子（技术交流群/技术交楼群）仍然是同音/形近字
REPLACEMENTS: list[tuple[str, str]] = [
    ("硅基妙妙屋", "技术交流群"),
    ("硅基炒饭屋", "技术交楼群"),
    ("柳伟杰", "林伟"),
    ("柳序杰", "林序"),
    ("Akihero", "Astero"),
    ("Akihiro", "Aster"),
    ("akihiro", "aster"),
    ("恸.", "Nox."),
    ("恸", "Nox"),
    ("慵.", "Lull."),
    # 上面两个群名换掉之后，文档里"是哪两个字读错了"这句话还指着旧字，
    # 会与改后的例子对不上（自相矛盾）。一并换掉，指回新的那两个字。
    ("炒饭", "交楼"),
    ("妙妙", "交流"),
]

# 只扫这些类型；二进制与构建产物不碰
SUFFIXES = {".md", ".py", ".json", ".swift", ".kt", ".yml", ".yaml", ".txt", ".sh", ".bat"}
SKIP_DIRS = {".git", "build", "dist", "__pycache__", "node_modules", ".zcode", "out"}


def targets() -> list[Path]:
    out: list[Path] = []
    # **必须排除本文件自己**：替换表里就写着真名，扫自己会把表改坏（自我破坏）。
    me = Path(__file__).resolve()
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.resolve() == me:
            continue
        out.append(p)
    return sorted(out)


def main() -> int:
    apply = "--apply" in sys.argv
    total = 0
    touched: list[tuple[str, int]] = []
    for p in targets():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = text
        hits = 0
        for real, fake in REPLACEMENTS:
            n = new.count(real)
            if n:
                new = new.replace(real, fake)
                hits += n
        if hits:
            rel = p.relative_to(ROOT).as_posix()
            touched.append((rel, hits))
            total += hits
            if apply:
                p.write_text(new, encoding="utf-8")

    mode = "已替换" if apply else "待替换（未改，加 --apply 才真改）"
    print(f"{mode}：{len(touched)} 个文件、{total} 处")
    for rel, hits in touched:
        print(f"  {hits:>3} 处  {rel}")

    # 复查：替换后不该再有真名
    if apply:
        left = []
        for p in targets():
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for real, _ in REPLACEMENTS:
                if real in text:
                    left.append((p.relative_to(ROOT).as_posix(), real))
        if left:
            print("\n!! 仍有残留：")
            for rel, real in left:
                print(f"  {rel}: {real}")
            return 1
        print("复查通过：全库已无真名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
