"""感知层：内存里的消息区图像 → 结构化对话（谁说的 + 说了什么）。

复用 tools/ios-spike/vlm_extract.py 的 prompt、容错解析与非文本处理，
但**不落盘**：那个模块只接受文件路径，而"截图不落盘"是本项目的红线
（聊天截图留在磁盘上比留在内存里危险得多）。这里直接从内存数组编 base64。

它拿到的也确实是截图，所以 prompt 里"手机聊天软件截图"的措辞对桌面版同样适用；
桌面上气泡也是左右分栏 + 自己的是绿色，prompt 要求的"不要只用左右判断"正好兜住
深色模式/自定义背景这些本地几何法会失效的情况。
"""

from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JEV_DIR = REPO / "tools" / "jev"
SPIKE = REPO / "tools" / "ios-spike"
for p in (str(JEV_DIR), str(SPIKE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import providers as pv            # noqa: E402
import vlm_extract as vx          # noqa: E402


def _data_url(rgb) -> str:
    """内存数组 → PNG 的 base64 data URL（不写磁盘）。"""
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def build_messages(rgb) -> list[dict]:
    """与 vlm_extract.build_messages 同构，但图来自内存。"""
    return [
        {"role": "system", "content": vx.SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "请把这张聊天截图还原成结构化 JSON。"},
                {"type": "image_url", "image_url": {"url": _data_url(rgb)}},
            ],
        },
    ]


def perceive(rgb, model: str | None = None, provider: str | None = None,
             timeout: float | None = None) -> tuple[list[tuple[str, str]], dict, dict]:
    """感知一次。返回 (消息列表[(side,text)], 结构化结果, meta)。

    消息列表里的 side 已收敛为 me / other，非文本消息转成
    [表情包：…] / [图片：…] / [语音消息] 这类方括号描述——
    这正是纯 OCR 方案拿不到、也决定了"对方只发一个表情包时能不能触发"的部分。
    """
    msgs = build_messages(rgb)
    t0 = time.time()
    content, usage, pinfo = pv.chat(
        msgs, role="perception", provider_override=provider, model=model,
        temperature=0, timeout=timeout,
    )
    elapsed = time.time() - t0
    structured = vx.parse_json_reply(content)
    jev_msgs = vx.to_jev_messages(structured, include_nontext=True)
    meta = {
        "provider": pinfo,
        "model": pinfo.get("model"),
        "elapsed_s": round(elapsed, 2),
        "usage": usage,
        "cost_usd": vx.estimate_cost(pinfo.get("model") or "", usage),
    }
    return jev_msgs, structured, meta


def describe_perception(provider: str | None = None) -> str:
    """给日志/UI 用的一行说明，不含密钥。"""
    return pv.describe("perception", provider)
