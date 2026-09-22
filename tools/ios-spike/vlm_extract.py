"""截图 → 结构化对话，交给多模态视觉模型（感知层）。

为什么把感知交给视觉模型，而不是本地 OCR + 启发式：
  1. 表情包/图片/语音/引用卡片，本地 OCR 读不到**任何**内容。实测那张真实截图里
     "我"发的全是猫图，本地 OCR 返回空；视觉模型能描述"一只黑猫躺着"，这是能力超集。
  2. "谁说的"由模型按语义判断（绿色气泡是我的、群聊看昵称），不依赖像素假设，
     深色模式、自定义聊天背景、飞书那种整体左对齐布局都能应付。
     本地原型实测：几何法 96.3%、颜色法 100%，但都只在浅色模式默认主题下成立。
  3. 不需要"消息区上下边界 / 气泡颜色阈值 / 折行合并"这些启发式。本地原型实测这些
     启发式会把无时间戳会话的首条消息、以及截图底部的最新消息误删。

端点和模型来自 tools/jev/providers.json 的 roles.perception（默认 OpenRouter 的
qwen3-vl）。想换供应商或换模型只改配置，不改这个文件。

成本控制（关键）：不要每帧都调模型。由本地免费的像素变化检测决定何时调用，
一次对话实际只有几次请求。

用法:
  export OPENROUTER_API_KEY=...
  python vlm_extract.py --image shot.png
  python vlm_extract.py --image shot.png --model qwen/qwen3-vl-8b-instruct --json out.json
  python vlm_extract.py --image shot.png --dry-run     # 只看请求大小，不发送
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SPIKE = Path(__file__).resolve().parent
JEV_DIR = SPIKE.parent / "jev"
sys.path.insert(0, str(JEV_DIR))

from providers import ProviderError, chat as provider_chat, describe, redact  # noqa: E402

MAX_HISTORY = 15  # 只取最近这些条，跟安卓版的 takeLast(10) 同量级

# 每 1M token 的价格（美元），仅用于成本估算；取自 OpenRouter /api/v1/models 的 pricing
PRICES = {
    "qwen/qwen3-vl-32b-instruct": (0.104, 0.416),
    "qwen/qwen3-vl-8b-instruct": (0.117, 0.455),
    "qwen/qwen3-vl-30b-a3b-instruct": (0.130, 0.520),
    "z-ai/glm-4.6v": (0.300, 0.900),
}

SYSTEM_PROMPT = """你是一个聊天截图结构化工具。输入是一张手机聊天软件（微信/飞书等）的截图，
你要把截图里的对话还原成结构化 JSON，供下游的判断模型使用。

判断"谁说的"（side）：
- 气泡在右侧、或气泡是绿色 → "me"（手机主人自己发的）
- 气泡在左侧、或气泡是白色/灰色 → "other"（对方发的）
- 群聊里同一侧有多个发言人，这些都属于 "other"，同时在 sender 里写明昵称
- 注意：不要用"左右"作为唯一依据，桌面版/飞书等布局可能整体左对齐，
  这时要靠气泡底色、头像位置、昵称标签综合判断。拿不准就按最可能的选择，并写进 notes。

消息内容（kind 与 text）：
- 文本消息：kind="text"，text 是完整原文。**折行的消息要合并成一条**，不要拆成多条。
- 表情包/贴图：kind="sticker"，text 留空，description 用一句话描述这个表情表达什么
- 图片：kind="image"，text 留空，description 一句话描述图片内容
- 语音：kind="voice"，text 留空，description 写"语音消息"
- 引用回复：kind="quote"，text 写回复正文，quote_text 写被引用的原文
- 红包/转账/收款：kind="payment"，text 留空，description 只写类别（如"红包"），
  **不要解读金额或催促点击**
- 系统提示（"你撤回了一条消息""对方正在输入"等）：kind="system"

必须忽略的（不要出现在 messages 里）：状态栏与聊天标题、时间戳、底部输入框与发送按钮、
输入框里还没发出去的草稿、截图时叠加在界面上的任何浮层或面板。

**尤其要忽略系统通知横幅与悬浮面板**——它们是本工具自己的输出，不是聊天内容。
特征：屏幕顶部或中部的圆角卡片，左侧有 App 图标，标题形如「Jev · 群名」，
正文含「安全 0/9」「留神 4/9」这类风险字样与「#1 / #2 / #3」编号的候选回复，
下面还可能有「候选 1 / 候选 2 / 候选 3」的按钮。
这类卡片上的文字**既不能当聊天消息，也不能当会话标题**。
实测踩过：通知浮在聊天上方时，标题被读成「Jev · Jev · Jev · 群名」，
正文里的候选回复被当成"对方最新消息"，整次判断全错。

只输出 JSON，不要 markdown 代码块，不要任何解释。格式：
{
  "title": "会话标题，没有就 null",
  "is_group": false,
  "messages": [
    {"side": "other", "kind": "text", "text": "在吗", "sender": null},
    {"side": "me", "kind": "text", "text": "在的"},
    {"side": "other", "kind": "sticker", "description": "一只柴犬大笑的表情包"}
  ],
  "notes": ["任何不确定之处"]
}
messages 按截图中从上到下的时间顺序排列，最多最近 15 条。"""


def build_messages(image_path: str | Path, include_nontext: bool = True) -> list[dict]:
    """构造 OpenAI 兼容的 messages。图片走 base64 data URL，不依赖图床。"""
    p = Path(image_path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    user_text = "请把这张聊天截图还原成结构化 JSON。"
    if not include_nontext:
        user_text += "只保留文本消息，忽略表情包/图片/语音。"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        },
    ]


def parse_json_reply(content: str) -> dict:
    """容错解析：剥掉 markdown 围栏，取第一个 {...} 块。"""
    s = (content or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ProviderError(f"模型返回的不是合法 JSON：\n{redact(content)[:800]}")


def estimate_cost(model: str, usage: dict | None) -> float | None:
    """按 usage 与实际价格估这次调用花了多少美元（价格表里没有就返回 None）。"""
    if not usage:
        return None
    pin, pout = PRICES.get(model or "", (0.0, 0.0))
    if not pin and not pout:
        return None
    return (usage.get("prompt_tokens", 0) or 0) / 1e6 * pin + (
        usage.get("completion_tokens", 0) or 0
    ) / 1e6 * pout


def to_jev_messages(structured: dict, include_nontext: bool = True) -> list[dict]:
    """转成给判断层用的消息列表。

    **保留发言人**（sender）。群聊里"对方"其实是多个人，压成一个 other 会让判断层
    以为所有话都是同一个人说的——它就没法知道"这个问题别人已经回答过了"。
    安卓版的一对一假设在群聊里不成立，根因就在这里。

    每条是 {"side": "me"|"other", "text": str, "sender": str|None, "kind": str}。
    非文本内容转成 [图片：...] 这样的方括号描述，让 Jev 也能把表情包/图片纳入判断
    ——这是安卓版结构上看不到的信息。
    """
    out: list[dict] = []
    for m in structured.get("messages") or []:
        side = "me" if (m.get("side") or "").strip() == "me" else "other"
        kind = (m.get("kind") or "text").strip()
        text = (m.get("text") or "").strip()
        sender = (m.get("sender") or "").strip() or None
        body = ""
        if kind == "text":
            body = text
        elif include_nontext:
            desc = (m.get("description") or "").strip()
            if kind == "sticker":
                body = f"[表情包：{desc}]" if desc else "[表情包]"
            elif kind == "image":
                body = f"[图片：{desc}]" if desc else "[图片]"
            elif kind == "voice":
                body = "[语音消息]"
            elif kind == "payment":
                body = f"[{desc or '红包/转账'}]"
            elif kind == "quote":
                qt = (m.get("quote_text") or "").strip()
                body = f"[引用「{qt}」] {text}" if qt else text
            elif kind == "system":
                continue
            else:
                body = text or f"[{kind}]"
        if body:
            out.append({"side": side, "text": body, "sender": sender, "kind": kind})
    return out[-MAX_HISTORY:]


def collect_senders(messages: list[dict]) -> list[str]:
    """按出现顺序收集对方阵营的发言人昵称（群聊用）。"""
    seen: list[str] = []
    for m in messages:
        s = m.get("sender")
        if s and m.get("side") != "me" and s not in seen:
            seen.append(s)
    return seen


def extract_vlm(
    image_path: str | Path,
    model: str | None = None,
    include_nontext: bool = True,
    timeout: float | None = None,
    provider: str | None = None,
) -> tuple[dict, dict]:
    """截图 → (结构化对话, meta)。provider/model 留空则取 providers.json 的 roles.perception。"""
    messages = build_messages(image_path, include_nontext)
    t0 = time.time()
    content, usage, pinfo = provider_chat(
        messages,
        role="perception",
        provider_override=provider,
        model=model,
        temperature=0,
        timeout=timeout,
        # 不写死 max_tokens：思考型模型需要更大预算，交给 provider 配置决定
    )
    elapsed = time.time() - t0
    structured = parse_json_reply(content)
    meta = {
        "provider": pinfo,
        "model": pinfo.get("model"),
        "elapsed_s": round(elapsed, 2),
        "usage": usage,
        "cost_usd": estimate_cost(pinfo.get("model") or "", usage),
    }
    return structured, meta


def main() -> int:
    ap = argparse.ArgumentParser(description="截图 → 结构化对话（多模态视觉模型）")
    ap.add_argument("--image", required=True)
    ap.add_argument("--model", default=None, help="覆盖 providers.json 里的模型")
    ap.add_argument("--provider", default=None, help="覆盖 roles.perception 指向的 provider")
    ap.add_argument("--json", dest="json_out", help="把结构化结果写成 JSON")
    ap.add_argument("--text-only", action="store_true", help="忽略表情包/图片/语音")
    ap.add_argument("--dry-run", action="store_true", help="只打印请求体大小，不发送")
    ap.add_argument("--raw", action="store_true", help="打印模型原始返回")
    args = ap.parse_args()

    include_nontext = not args.text_only
    messages = build_messages(args.image, include_nontext)

    if args.dry_run:
        img_len = len(messages[1]["content"][1]["image_url"]["url"])
        print(describe("perception", args.provider))
        print(f"system prompt: {len(SYSTEM_PROMPT)} 字符")
        print(f"图片 base64: {img_len} 字符（约 {img_len * 3 / 4 / 1024:.0f} KB）")
        print("未发送（--dry-run）")
        return 0

    structured, meta = extract_vlm(
        args.image, args.model, include_nontext, provider=args.provider
    )
    if args.raw:
        print(json.dumps(structured, ensure_ascii=False, indent=2))

    u = meta["usage"]
    cost = meta["cost_usd"]
    cost_s = f"${cost:.6f}" if cost is not None else "未知"
    print(f"provider: {meta['provider'].get('id')}   模型: {meta['model']}   耗时: {meta['elapsed_s']}s")
    print(
        f"token: 输入 {u.get('prompt_tokens', '?')} / 输出 {u.get('completion_tokens', '?')}"
        f"   本次花费 {cost_s}"
    )
    print(f"标题: {structured.get('title')!r}   群聊: {structured.get('is_group')}")
    print("-" * 60)
    for m in structured.get("messages") or []:
        tag = "我" if (m.get("side") or "") == "me" else "对方"
        print(f"  [{tag}] ({m.get('kind')}) {m.get('text') or m.get('description') or ''}")
    notes = structured.get("notes") or []
    if notes:
        print("\n模型标注的不确定处:")
        for n in notes:
            print(f"  - {n}")

    msgs = to_jev_messages(structured, include_nontext)
    senders = collect_senders(msgs)
    print(f"\n送给判断层的消息 {len(msgs)} 条" + (f"，发言人 {len(senders)} 位: {senders}" if senders else ""))
    for m in msgs:
        tag = "我  " if m["side"] == "me" else "对方"
        who = f"({m['sender']})" if m.get("sender") else ""
        print(f"  {tag}{who} {m['text']}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"structured": structured, "meta": meta, "jev_messages": msgs},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已写 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
