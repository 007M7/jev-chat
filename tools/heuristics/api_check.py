#!/usr/bin/env python3
"""跨文件符号检查：视图里访问的 `store.x` / `bridge.x` / `capture.x` / `JevPipeline.X` 是否真的存在。

为什么需要：本机没有 Swift 工具链（Windows），跨文件符号错误只能等云构建发现，
而云构建要排队 10 分钟起步。这类错误（拼错成员名、用了一个还没加的常量、
把 func 当属性用）恰好是**纯文本可判**的，本地几毫秒就能挡掉。

它不假装是编译器，只做三件确定的事：
  1. 访问的成员名在该类型里根本没声明；
  2. 声明是 func，却被当属性用（或反之）；
  3. 访问的嵌套类型（如 JevPipeline.SessionHint）不存在。

为此要小心三件容易误报的事，都踩过：
  - `@Published private(set) var x` 的 `private(set)` 会让朴素的修饰符正则失配；
  - 成员名会重复（`StoredAnalysis.sessionKey` 是属性，`AnalysisStore.sessionKey(for:)` 是方法），
    所以一个名字要收**一组**可能的 kind，不能先到先得；
  - 字符串字面量里会出现 `capture.frames` 这种假访问，必须先剥掉字符串和注释。

退出码 1 表示发现问题。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 被访问方 → (类型名, 声明所在文件, 哪些目录里这个别名才是那个共享对象)
PROVIDERS: dict[str, tuple[str, Path, str]] = {
    "store": ("AnalysisStore", ROOT / "ios/Sources/Brain/AnalysisStore.swift", "App"),
    "bridge": ("AppBridge", ROOT / "ios/Sources/App/JevApp.swift", "App"),
    "capture": ("CaptureController", ROOT / "ios/Sources/Capture/CaptureController.swift", "App"),
    "JevPipeline": ("JevPipeline", ROOT / "ios/Sources/Brain/JevPipeline.swift", ""),
}

INHERITED = {"objectWillChange", "shared", "description", "debugDescription", "hashValue"}

DECL_KINDS = ("func", "var", "let", "struct", "enum", "class", "typealias", "actor")
# 类型声明（用于"嵌套类型被当顶层裸用"检查）
TYPE_RE = re.compile(r"^(struct|enum|class|actor|protocol|typealias)\s+([A-Za-z_][A-Za-z0-9_]*)")
MODIFIERS = {
    "public", "internal", "private", "fileprivate", "open", "final", "static",
    "class", "nonisolated", "override", "mutating", "indirect", "lazy", "weak",
    "unowned", "convenience", "required", "dynamic", "(set)", "(get)",
}
DECL_RE = re.compile(r"^(func|var|let|struct|enum|class|typealias|actor)\s+([A-Za-z_][A-Za-z0-9_]*)")
ATTR_RE = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*(?:\([^()]*\))?")

ACCESS_RE = re.compile(r"\b(" + "|".join(PROVIDERS) + r")\.([A-Za-z_][A-Za-z0-9_]*)")


def strip_literals_and_comments(text: str) -> str:
    """把字符串字面量和注释换成空格，保持行号与列宽不变。"""
    out = []
    i, n, in_block = 0, len(text), False
    while i < n:
        if in_block:
            if text.startswith("*/", i):
                out.append("  ")
                i += 2
                in_block = False
            else:
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            continue
        if text.startswith("/*", i):
            out.append("  ")
            i += 2
            in_block = True
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if text[i] == '"':
            # 三引号 / 普通字符串，都按"读到未转义的收尾引号"处理
            triple = text.startswith('"""', i)
            j = i + (3 if triple else 1)
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if triple and text.startswith('"""', j):
                    j += 3
                    break
                if not triple and text[j] == '"':
                    j += 1
                    break
                if not triple and text[j] == "\n":
                    break  # 未闭合，认了
                j += 1
            out.append("".join("\n" if c == "\n" else " " for c in text[i:j]))
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def declared_members(path: Path) -> dict[str, set[str]]:
    """{成员名: {kind, ...}}，只看缩进恰好 4 空格的一层成员。"""
    out: dict[str, set[str]] = {}
    if not path.exists():
        return out
    src = strip_literals_and_comments(path.read_text(encoding="utf-8"))
    for line in src.splitlines():
        if not line.startswith("    ") or line.startswith("     "):
            continue
        rest = line[4:]
        # 逐段剥掉 @属性 和修饰符，直到露出声明关键字
        while True:
            m = ATTR_RE.match(rest)
            if m:
                rest = rest[m.end():].lstrip()
                continue
            head = rest.split(" ", 1)[0].split("(", 1)[0]
            if head in MODIFIERS and rest:
                cut = rest.find(" ")
                if cut < 0:
                    break
                rest = rest[cut + 1:].lstrip()
                continue
            break
        m = DECL_RE.match(rest)
        if m:
            out.setdefault(m.group(2), set()).add(m.group(1))
    return out


def type_scopes(files: list[Path]) -> tuple[dict[str, str], dict[str, str]]:
    """收集类型名的声明位置：返回 (顶层类型 → 文件, 嵌套类型 → 文件)。

    为什么要查这个：`struct ChatSession` 写在 AnalysisStore 类体里（缩进 4）就变成
    嵌套类型，别的文件里裸写 `ChatSession` 会"找不到类型"。本机没有编译器，
    这种错只能等云构建——而它恰好是纯文本可判的。
    """
    top: dict[str, str] = {}
    nested: dict[str, str] = {}
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        src = strip_literals_and_comments(f.read_text(encoding="utf-8"))
        for line in src.splitlines():
            m = TYPE_RE.match(line)
            if m:
                top.setdefault(m.group(2), rel)
                continue
            m2 = TYPE_RE.match(line.strip()) if line.startswith(("    ", "        ")) else None
            if m2:
                nested.setdefault(m2.group(2), rel)
    return top, nested


def main() -> int:
    problems: list[str] = []
    tables = {a: (t, declared_members(p), prefix) for a, (t, p, prefix) in PROVIDERS.items()}
    for alias, (tname, table, _) in tables.items():
        if not table:
            problems.append(f"{alias}: 读不到 {tname} 的成员声明（{PROVIDERS[alias][1]} 不存在或格式变了）")

    files = sorted((ROOT / "ios/Sources").rglob("*.swift"))
    checked = 0
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        src = strip_literals_and_comments(f.read_text(encoding="utf-8"))
        for lineno, line in enumerate(src.splitlines(), 1):
            for m in ACCESS_RE.finditer(line):
                alias, member = m.group(1), m.group(2)
                tname, table, scope = tables[alias]
                if scope and f"/{scope}/" not in "/" + rel:
                    continue  # 这个文件里的 `capture` 是别的东西，不是我们的单例
                if member in INHERITED or not table:
                    continue
                checked += 1
                kinds = table.get(member)
                if not kinds:
                    problems.append(f"{rel}:{lineno}: {alias}.{member} 未在 {tname} 里声明")
                    continue
                called = line[m.end():m.end() + 1] == "("
                # 类型名后面跟 () 是合法的（合成初始化器），不算"当函数用"
                type_like = kinds & {"struct", "enum", "class", "actor", "typealias"}
                if called and "func" not in kinds and not type_like:
                    problems.append(
                        f"{rel}:{lineno}: {alias}.{member} 是 {'/'.join(sorted(kinds))}，但被当函数调用")
                elif not called and kinds == {"func"}:
                    problems.append(f"{rel}:{lineno}: {alias}.{member} 是 func，但被当属性用")

    print(f"检查了 {len(files)} 个文件、{checked} 处成员访问")

    # ---- 嵌套类型 vs 顶层类型的用法 ----
    top, nested = type_scopes(files)
    nested_only = {k: v for k, v in nested.items() if k not in top}
    type_hits = 0
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        src = strip_literals_and_comments(f.read_text(encoding="utf-8"))
        for lineno, line in enumerate(src.splitlines(), 1):
            for name, where in nested_only.items():
                # 裸用（前面不是 `.`）——嵌套类型必须在自己的文件里、或带 Owner. 前缀
                if re.search(r"(?<![.\w])" + name + r"\b", line) and rel != where:
                    type_hits += 1
                    problems.append(
                        f"{rel}:{lineno}: {name} 是嵌套类型（声明在 {where}），"
                        f"跨文件使用要写全 Owner.{name}，或把它挪到顶层")
            for owner_m in re.finditer(r"\b([A-Z][A-Za-z0-9_]*)\.([A-Z][A-Za-z0-9_]*)", line):
                owner, member = owner_m.group(1), owner_m.group(2)
                if member in top and member not in nested:
                    type_hits += 1
                    problems.append(
                        f"{rel}:{lineno}: {owner}.{member} 里的 {member} 是顶层类型，"
                        f"不是 {owner} 的嵌套类型（去掉 {owner}. 前缀）")
    print(f"另外检查了 {len(nested_only)} 个嵌套类型名的跨文件用法、{type_hits} 处命中")

    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print("  " + p)
        return 1
    print("未发现未声明的跨文件成员访问")
    return 0


if __name__ == "__main__":
    sys.exit(main())
