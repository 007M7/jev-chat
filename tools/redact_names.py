#!/usr/bin/env python3
"""把仓库里出现的真实昵称/群名换成伪名（映射表放在**仓库外**，见下）。

为什么需要它：这个仓库是**公开**的，而文档与示例数据里带着真实聊天记录里的
群名和人名（视觉模型的实测样例、别名归并的测试序列、UI 截图里抄下来的文案）。
这些不该出现在公开仓库里。

为什么写成脚本而不是手改：替换必须**全库一致**——只改一半会让示例自相矛盾
（比如别名归并的例子只剩一边），而且下次还会漏。跑一次脚本，剩下的都是伪名。

**映射表刻意不放本文件**：本文件要提交进公开仓库，而映射表的「真名」那一列
就是待脱敏的内容——写进来等于把要藏的东西又发布一遍（这个坑踩过：第一版
就是这么提交的，等于是自伤）。所以映射表放在这里：

    tools/redact_names.local.json      ← 已加进 .gitignore，不入库

格式（一个 JSON 数组，长的放前面，先替换长的）：

    { "replacements": [["真名甲", "伪名甲"], ["真名乙", "伪名乙"]] }

用法：
  python tools/redact_names.py                       # 只报告，不改
  python tools/redact_names.py --apply               # 真的改
  python tools/redact_names.py --map 其它路径.json    # 换一份映射表
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / "local" / "redact_names.local.json"

# 只扫这些类型；二进制与构建产物不碰
SUFFIXES = {".md", ".py", ".json", ".swift", ".kt", ".yml", ".yaml", ".txt", ".sh", ".bat"}
SKIP_DIRS = {".git", "build", "dist", "__pycache__", "node_modules", ".zcode", "out"}


def load_map(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        raise SystemExit("\n".join([
            f"缺少映射表：{path}",
            "",
            "  这是**故意**不入库的：映射表里写着真名，而本文件要提交进公开仓库。",
            "  自己建一份即可（格式）：",
            '    { "replacements": [["真名甲", "伪名甲"], ["真名乙", "伪名乙"]] }',
            "",
            "  注意长的放前面：先替换「张三丰」再替换「张三」，否则会留下一半。",
        ]))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"映射表不是合法 JSON：{path}\n  {exc}")
    pairs = data.get("replacements") if isinstance(data, dict) else data
    if not isinstance(pairs, list) or not pairs:
        raise SystemExit(f"映射表里没有 replacements：{path}")
    out: list[tuple[str, str]] = []
    for item in pairs:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise SystemExit(f"replacements 的每一项都应是 [真名, 伪名]：{item!r}")
        out.append((str(item[0]), str(item[1])))
    return out


def targets(map_path: Path) -> list[Path]:
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        # 本文件自己：注释里举例用的名字可能被换掉，没必要冒险
        if p.resolve() == Path(__file__).resolve():
            continue
        # 映射表本身：里面就是真名，当然"命中"，报出来只是噪声
        if p.resolve() == map_path.resolve():
            continue
        out.append(p)
    return sorted(out)


def main() -> int:
    args = sys.argv[1:]
    apply = "--apply" in args
    map_path = DEFAULT_MAP
    if "--map" in args:
        i = args.index("--map")
        if i + 1 >= len(args):
            raise SystemExit("--map 后面要给路径")
        map_path = Path(args[i + 1])

    replacements = load_map(map_path)
    total = 0
    touched: list[tuple[str, int]] = []
    for p in targets(map_path):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new = text
        hits = 0
        for real, fake in replacements:
            n = new.count(real)
            if n:
                new = new.replace(real, fake)
                hits += n
        if hits:
            touched.append((p.relative_to(ROOT).as_posix(), hits))
            total += hits
            if apply:
                p.write_text(new, encoding="utf-8")

    mode = "已替换" if apply else "待替换（未改，加 --apply 才真改）"
    print(f"{mode}：{len(touched)} 个文件、{total} 处")
    for rel, hits in touched:
        print(f"  {hits:>3} 处  {rel}")

    if apply:
        left = []
        for p in targets(map_path):
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for real, _ in replacements:
                if real in text:
                    left.append((p.relative_to(ROOT).as_posix(), real))
        if left:
            print("\n!! 仍有残留（映射表里可能缺了变体写法）：")
            for rel, real in left:
                print(f"  {rel}: {real}")
            return 1
        print("复查通过：全库已无映射表里的真名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
