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

# 被访问的别名 → 该别名"通常是哪个类型"（**只是兜底**）。
#
# 为什么需要兜底而不是写死：同一个名字在不同文件里指的不是同一个类型。
# 例如 `store` 在记录页是 AnalysisStore、在 provider 编辑页是 ProvidersStore。
# 写死会直接误报——这个坑踩过：新加 ProvidersStore 之后，检查器把 ProvidersView 里
# 每一处 store.xxx 都报成"未在 AnalysisStore 里声明"，几十条假问题。
# 所以优先**按文件解析**别名的真实类型（见 alias_types），解析不出来才用这里。
DEFAULT_TYPE: dict[str, str] = {
    "store": "AnalysisStore",
    "bridge": "AppBridge",
    "capture": "CaptureController",
    "JevPipeline": "JevPipeline",
}

# 只有这些别名会被检查（避免把任意 obj.member 都拿来解析）
ALIASES = tuple(DEFAULT_TYPE)

INHERITED = {"objectWillChange", "shared", "description", "debugDescription", "hashValue"}

DECL_KINDS = ("func", "var", "let", "struct", "enum", "class", "typealias", "actor")
# 类型声明（用于"嵌套类型被当顶层裸用"检查）
TYPE_RE = re.compile(r"^(struct|enum|class|actor|protocol|typealias)\s+([A-Za-z_][A-Za-z0-9_]*)")
MODIFIERS = {
    "public", "internal", "private", "fileprivate", "open", "final", "static",
    "class", "nonisolated", "override", "mutating", "indirect", "lazy", "weak",
    "unowned", "convenience", "required", "dynamic", "(set)", "(get)",
}
SETTER_RE = re.compile(r"^(?:public|private|internal|fileprivate|open)\(set\)$")
EXT_RE = re.compile(r"^extension\s+([A-Za-z_][A-Za-z0-9_.]*)")
DECL_RE = re.compile(r"^(func|var|let|struct|enum|class|typealias|actor)\s+([A-Za-z_][A-Za-z0-9_]*)")
ATTR_RE = re.compile(r"^@[A-Za-z_][A-Za-z0-9_]*(?:\([^()]*\))?")

ACCESS_RE = re.compile(r"\b(" + "|".join(ALIASES) + r")\.([A-Za-z_][A-Za-z0-9_]*)")

# 别名在某个文件里的类型来自哪：
#   @ObservedObject private var store = AnalysisStore.shared
#   @ObservedObject var store: ProvidersStore
#   private let capture = CaptureController()
ALIAS_DECL_RE = re.compile(
    r"^\s*(?:(?:@\w+(?:\([^()]*\))?"
    r"|(?:public|internal|private|fileprivate|open|final|static|class|nonisolated|override|lazy|weak|unowned|mutating|indirect)\b"
    r"|\(set\)|\(get\))\s+)*"
    r"(?:var|let)\s+(" + "|".join(ALIASES) + r")\s*(?::\s*([A-Za-z_][A-Za-z0-9_.]*)|=\s*([A-Za-z_][A-Za-z0-9_]*))")

def alias_types(src: str) -> dict[str, str]:
    """这个文件里 `store` / `bridge` / … 各是什么类型。

    两种写法都认：类型标注（`var store: ProvidersStore`）与初始化式
    （`var store = ProvidersStore.shared`）。后者常见于 `@ObservedObject`。
    """
    out: dict[str, str] = {}
    for line in src.splitlines():
        m = ALIAS_DECL_RE.match(line)
        if not m:
            continue
        alias = m.group(1)
        typ = m.group(2) or m.group(3) or ""
        # `= AnalysisStore.shared` 会匹配到 AnalysisStore；去模块前缀与泛型
        typ = typ.split("<")[0].strip()
        if typ in ALIASES:      # 形如 var store = store，忽略
            continue
        if typ and alias not in out:
            out[alias] = typ
    return out


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


def strip_modifiers(line: str) -> str:
    """逐段剥掉 @属性 与修饰符，露出声明关键字开头的部分。

    坑：`class` 既可能是类型声明（`class Foo`）又可能是修饰符（`class func`）。
    一律当修饰符剥掉的话，`final class AnalysisStore {` 会被剥成
    `AnalysisStore: ObservableObject {`，于是**所有类型都认不出来**（踩过）。
    所以 `class` 只在后面跟 func/var/let 时才当修饰符。
    """
    rest = line.lstrip()
    while True:
        m = ATTR_RE.match(rest)
        if m:
            rest = rest[m.end():].lstrip()
            continue
        parts = rest.split()
        if not parts:
            break
        head = parts[0]
        # private(set) / public(set)：带括号的访问级别修饰符，正则式的 MODIFIERS 覆盖不到
        if SETTER_RE.match(head):
            rest = rest[len(head):].lstrip()
            continue
        if head in DECL_KINDS:
            if head == "class" and len(parts) > 1 and parts[1] in ("func", "var", "let"):
                rest = rest[len(head):].lstrip()
                continue
            break
        if head in MODIFIERS:
            rest = rest[len(head):].lstrip()
            continue
        break
    return rest


def collect_members(path: Path) -> dict[str, dict[str, set[str]]]:
    """扫一个文件，返回 {类型名: {成员名: {kind}}}。

    **按缩进栈归位**：成员挂在包含它的那个类型上，而不是"所有缩进 4 行的都算
    AnalysisStore 的成员"。这一步是必要的——写死类型名会让新加的类型全部误报
    （ProvidersStore 加进来时踩过）。
    """
    out: dict[str, dict[str, set[str]]] = {}
    if not path.exists():
        return out
    src = strip_literals_and_comments(path.read_text(encoding="utf-8"))
    stack: list[tuple[str, int]] = []      # (类型名, 声明行的缩进)

    def add(type_name: str, member: str, kind: str) -> None:
        out.setdefault(type_name, {}).setdefault(member, set()).add(kind)

    for line in src.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = strip_modifiers(line)
        # 退出已经结束的作用域
        while stack and stack[-1][1] >= indent:
            stack.pop()

        # `extension Foo { … }`：成员属于 Foo，当成它的作用域继续收集。
        # 漏掉扩展会让"在扩展里声明的成员"被判成未声明——那是假问题。
        ext = EXT_RE.match(body)
        if ext:
            stack.append((ext.group(1).split(".")[-1], indent))
            continue

        m = DECL_RE.match(body)
        if not m:
            continue
        kind, name = m.group(1), m.group(2)

        if stack and indent > stack[-1][1]:
            add(stack[-1][0], name, kind)
        if kind in ("struct", "enum", "class", "actor", "protocol", "typealias"):
            stack.append((name, indent))
            out.setdefault(name, {})
    return out


def declared_members(path: Path, type_name: str) -> dict[str, set[str]]:
    return collect_members(path).get(type_name, {})


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
    files = sorted((ROOT / "ios/Sources").rglob("*.swift"))

    # 所有文件的所有类型成员表。别名指向哪个类型**按文件解析**，不写死。
    members_by_file: dict[str, dict[str, dict[str, set[str]]]] = {}
    all_types: dict[str, dict[str, set[str]]] = {}
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        table = collect_members(f)
        members_by_file[rel] = table
        for tname, members in table.items():
            all_types.setdefault(tname, {}).update(members)

    missing_default = [a for a, t in DEFAULT_TYPE.items() if t not in all_types]
    for alias in missing_default:
        problems.append(
            f"兜底类型 {DEFAULT_TYPE[alias]}（别名 {alias}）在任何文件里都找不到成员声明")

    checked = 0
    resolved_files = 0
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        src = strip_literals_and_comments(f.read_text(encoding="utf-8"))
        local = alias_types(src)
        if local:
            resolved_files += 1
        for lineno, line in enumerate(src.splitlines(), 1):
            for m in ACCESS_RE.finditer(line):
                alias, member = m.group(1), m.group(2)
                # 优先用本文件声明的类型；没有声明才退回兜底类型
                tname = local.get(alias) or DEFAULT_TYPE[alias]
                table = all_types.get(tname)
                if not table or member in INHERITED:
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

    print(f"检查了 {len(files)} 个文件、{checked} 处成员访问"
          f"（{resolved_files} 个文件里的别名按本文件声明的类型解析，其余用兜底类型）")

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
