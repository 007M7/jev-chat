"""Swift 源码的本地静态检查（没有 mac/Xcode 时的替代品）。

为什么需要：iOS 端只能在云构建上编译，一轮要 1~3 分钟，而有些错误纯文本就能判定。
实测已为「变量在声明之前被使用」这类错误浪费了三轮 CI，所以值得有个本地检查。

**它不是编译器**，只覆盖一类错误：**同一个函数体内，局部变量在声明行之前被引用**。
刻意做得"宁可漏报、不可误报"：
  - 用**缩进**精确划分函数作用域（不靠大括号计数——嵌套函数与闭包会让计数失真）
  - 嵌套函数/闭包的变量只归属它自己的作用域，不并入外层（否则 score() 里的 probs
    会被误判成外层 use-before-declaration，实测踩过这个假阳性）
  - 只查同一作用域内的前后顺序，不查闭包捕获（那需要真正的语义分析）

用法:
  python tools/heuristics/swift_check.py            # 默认查 ios/
  python tools/heuristics/swift_check.py --path ios/Sources
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent.parent

FUNC_DECL = re.compile(r"^(\s*)(?:@\w+\s+)*(?:private |public |internal |fileprivate |static |final |"
                       r"override |nonisolated |mutating |@MainActor |@escaping )*func\s+([A-Za-z_][A-Za-z0-9_]*)\s*[<(]")
DECL = re.compile(r"^(\s*)(?:let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=")
IDENT = re.compile(r"\b([a-zA-Z_][A-Za-z0-9_]*)\b")

# 这些名字在函数体外也有定义，出现即不算局部变量问题
BUILTINS = {
    "self", "true", "false", "nil", "return", "if", "else", "guard", "let", "var", "func",
    "for", "while", "in", "switch", "case", "default", "break", "continue", "try", "await",
    "throw", "throws", "async", "as", "is", "do", "catch", "defer", "repeat", "where",
    "init", "enum", "struct", "class", "extension", "protocol", "import", "typealias",
}


def strip_comments_and_strings(text: str) -> list[str]:
    """把字符串内容与注释替换成占位，保留行数与缩进（缩进用于作用域判定，必须保住）。"""
    out: list[str] = []
    in_block = False
    for line in text.split("\n"):
        res: list[str] = []
        i = 0
        in_str = False
        while i < len(line):
            ch = line[i]
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if in_block:
                if ch == "*" and nxt == "/":
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
                    res.append('"')
                i += 1
                continue
            if ch == "/" and nxt == "/":
                break
            if ch == "/" and nxt == "*":
                in_block = True
                i += 2
                continue
            if ch == '"':
                in_str = True
                res.append('"')
                i += 1
                continue
            res.append(ch)
            i += 1
        out.append("".join(res))
    return out


def check_file(path: Path) -> tuple[list[str], int]:
    lines = strip_comments_and_strings(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    checked_funcs = 0

    # 找出所有函数声明，并按缩进确定各自的作用域范围
    funcs: list[dict] = []
    for idx, line in enumerate(lines):
        m = FUNC_DECL.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        name = m.group(2)
        # 作用域 = 后续所有缩进 > indent 的行，直到出现缩进 <= indent 的非空行
        end = idx + 1
        for j in range(idx + 1, len(lines)):
            if lines[j].strip() and (len(lines[j]) - len(lines[j].lstrip())) <= indent:
                break
            end = j + 1
        funcs.append({"name": name, "indent": indent, "start": idx, "end": end})

    for f in funcs:
        checked_funcs += 1
        indent = f["indent"]
        nested = [g for g in funcs if g["start"] > f["start"] and g["end"] <= f["end"]]

        # 函数体基础缩进 = 体内非空行的最小缩进。
        # **只检查基础缩进层的声明**：for/if/while 块里的声明属于更小的作用域，
        # 归到函数体会误报（实测把 for 循环里的 side/text/sender 报成先用后声明）。
        base = None
        for j in range(f["start"] + 1, f["end"]):
            if lines[j].strip():
                li = len(lines[j]) - len(lines[j].lstrip())
                base = li if base is None else min(base, li)
        if base is None:
            continue

        decls: dict[str, int] = {}
        for j in range(f["start"] + 1, f["end"]):
            line = lines[j]
            if not line.strip():
                continue
            li = len(line) - len(line.lstrip())
            if li != base:
                continue                      # 嵌套块（for/if/闭包）里的声明，跳过
            if any(g["start"] <= j < g["end"] for g in nested):
                continue                      # 嵌套函数，跳过
            d = DECL.match(line)
            if d:
                decls.setdefault(d.group(2), j + 1)

        # 检查：声明行之前是否引用了该名字（同样只看基础缩进层，避免把嵌套块里的
        # 同名变量算进来）
        for name, decl_line in decls.items():
            for j in range(f["start"] + 1, decl_line - 1):
                line = lines[j]
                if not line.strip():
                    continue
                li = len(line) - len(line.lstrip())
                if li < base:
                    continue
                if any(g["start"] <= j < g["end"] for g in nested):
                    continue
                if re.search(r"\b" + re.escape(name) + r"\b", line):
                    problems.append(
                        f"{path}:{j + 1}: 函数 {f['name']}() 在第 {decl_line} 行声明 {name} 之前就引用了它"
                    )
    return problems, checked_funcs


def main() -> int:
    ap = argparse.ArgumentParser(description="Swift 本地静态检查（无编译器时的替代）")
    ap.add_argument("--path", default="ios", help="要检查的目录（默认 ios）")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.is_absolute():
        root = REPO / args.path
    files = sorted(root.rglob("*.swift"))
    if not files:
        print(f"没找到 .swift 文件：{root}")
        return 2

    all_problems: list[str] = []
    total_funcs = 0
    for f in files:
        probs, n = check_file(f)
        all_problems.extend(probs)
        total_funcs += n

    print(f"检查了 {len(files)} 个文件、{total_funcs} 个函数")
    if all_problems:
        print(f"\n发现 {len(all_problems)} 处「先用后声明」：")
        for p in all_problems:
            print("  ✗", p)
        return 1
    print("未发现「局部变量先用后声明」")
    print()
    print("注意：这不是编译器。语法错误、类型错误、跨文件符号只靠云构建发现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
