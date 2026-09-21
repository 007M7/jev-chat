"""perception prompt 的单一来源校验。

问题：感知层的 system prompt 现在有三处使用者——Python 原型、桌面版、iOS(Swift)。
Swift 读不了 Python 模块，最容易变成手抄的第三份副本，然后必然漂移。
做法与 check_questions.py 一致：prompts.json 是唯一来源，本脚本断言 Python 侧与它一致。

用法:
  python check_prompts.py --write     # 从 vlm_extract.py 导出 prompts.json
  python check_prompts.py             # 校验（不一致返回 1）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
PROMPTS = ROOT / "prompts.json"
SPIKE = REPO / "tools" / "ios-spike"
SWIFT_PERCEPTION = REPO / "ios" / "Sources" / "Brain" / "PerceptionClient.swift"


def load_python_prompt() -> str:
    sys.path.insert(0, str(SPIKE))
    import vlm_extract as vx  # noqa: E402

    return vx.SYSTEM_PROMPT


def write_canonical() -> int:
    sys.path.insert(0, str(SPIKE))
    import vlm_extract as vx  # noqa: E402

    data = {
        "_comment": (
            "感知层 prompt 的单一来源。Python(tools/ios-spike/vlm_extract.py)、"
            "桌面版(desktop/perceive.py)、iOS(Swift 侧读这个文件) 都必须与它一致，"
            "用 tools/jev/check_prompts.py 校验——三份手抄副本必然漂移。"
        ),
        "version": 1,
        "perception": {
            "system_prompt": vx.SYSTEM_PROMPT,
            "user_text": "请把这张聊天截图还原成结构化 JSON。",
            "user_text_textonly": "请把这张聊天截图还原成结构化 JSON。只保留文本消息，忽略表情包/图片/语音。",
            "max_messages": vx.MAX_HISTORY,
        },
    }
    PROMPTS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写 {PROMPTS}")
    return 0


def verify() -> int:
    if not PROMPTS.exists():
        print(f"缺少 {PROMPTS.name}，先跑 python check_prompts.py --write")
        return 2
    canon = json.loads(PROMPTS.read_text(encoding="utf-8"))
    expected = canon["perception"]["system_prompt"]
    actual = load_python_prompt()

    problems: list[str] = []
    if actual != expected:
        problems.append(
            f"prompts.json 与 vlm_extract.SYSTEM_PROMPT 不一致"
            f"（json {len(expected)} 字符 / python {len(actual)} 字符）"
        )

    checked = 1
    # Swift 侧只检查"是否从 bundle 读"，不重复 prompt 正文——
    # 正文在 Swift 里出现即说明有人手抄了，那才是要防的。
    if SWIFT_PERCEPTION.exists():
        src = SWIFT_PERCEPTION.read_text(encoding="utf-8")
        checked += 1
        if "prompts.json" not in src and "prompts" not in src:
            problems.append("PerceptionClient.swift 没有从 prompts.json 读取 prompt（可能手抄了正文）")
        # 抓 prompt 正文的特征片段，出现在 Swift 里就是手抄
        fingerprint = "你是一个聊天截图结构化工具"
        if fingerprint in src:
            problems.append("PerceptionClient.swift 里出现了 prompt 正文——必须改成从 prompts.json 读，不要手抄")

    print(f"校验项: {checked}")
    print(f"prompt: {len(expected)} 字符")
    if problems:
        print(f"\n不一致 {len(problems)} 处：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n结论: prompts.json 与 Python 侧一致，Swift 侧走 bundle 读取")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="perception prompt 单一来源校验")
    ap.add_argument("--write", action="store_true", help="从 vlm_extract.py 导出")
    args = ap.parse_args()
    return write_canonical() if args.write else verify()


if __name__ == "__main__":
    raise SystemExit(main())
