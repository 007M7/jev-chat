"""按群组 / 联系人维护长期 memory 档案。

## 为什么这层能提高准确度（不是锦上添花）

实测到的缺陷：同一套题目在一对一标注集上跨题矛盾 0%，用在真实群聊上就冒出
「要你办事」+「需要行动」+「不必给实质内容」这种自相矛盾的组合。根因是**关系前提靠猜**——
题目集预设了"对方在测试你在不在乎"，而群聊里没有这层关系。

memory 档案解决的就是这个：把"这个群是什么、说话的人是谁、我和他是什么关系、
我答应过他什么"变成**已知输入**，而不是让判断模型去猜。这是准确度提升的主要来源。

## 四条硬规矩（违反任何一条都会让记忆变成负债）

1. **每条记忆必须带出处**：`evidence` 是消息原文片段。没有出处的不许入库。
   一条记错的"事实"比没有记忆更糟——它会稳定地把判断带偏，且没人会发现。
2. **只存可核对的事实，不存推断**：存「11月3日他说'你最好记住'」，
   不存「他很在意我记不记得」。推断留给判断层每轮重算。
3. **结构化字段，不是自由文本摘要**：自由文本没法按 key 覆盖、没法设过期、没法审计。
4. **有生命周期**：承诺类（commitment）到期即失效，避免拿三个月前的承诺当现在的事实。

## 存储

原型阶段写在 `tools/ios-spike/out/memory/`（已被 .gitignore 排除，含真实聊天内容）。
真机版应存在设备本地（iOS 用 App 沙盒 + Keychain 加密），不上传。

用法:
  python memory.py --list                                  # 看有哪些档案
  python memory.py --show "群名"                            # 看某个档案
  python memory.py --distill <pipeline结果.json>             # 从一次分析结果蒸馏记忆
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = ROOT.parent / "ios-spike" / "out" / "memory"

# 记忆类型。kind 决定它怎么被使用、以及该不该过期。
KINDS = (
    "identity",    # 对方是谁、和我的关系、角色（如：项目合伙人 / 潜在用户 / 家人）
    "fact",        # 关于对方或这个群的稳定事实（如：他在做 Android 客户端）
    "commitment",  # 我答应过的事，或对方答应过的事（应设 expires）
    "preference",  # 对方的偏好或忌讳（如：不喜欢被公开催）
    "boundary",    # 不能碰的话题（如：别在群里提钱）
)


@dataclass
class MemoryItem:
    """一条记忆。key 用来覆盖更新，evidence 用来审计。

    `about` 是这条记忆**关于谁**——群聊里身份类事实必须能落到具体的人身上，
    否则"这个群的人"会被当成一个模糊整体（这是用户明确提出的要求：
    群聊场景要能区分不同的人）。`source_sender` 是**谁说的**，两者不同。
    """

    key: str
    kind: str
    text: str
    evidence: str
    source_chat: str
    source_sender: str | None = None
    about: str | None = None
    first_seen: str = ""
    last_seen: str = ""
    expires: str | None = None
    confidence: float = 0.7

    def expired(self, today: str | None = None) -> bool:
        if not self.expires:
            return False
        return (today or date.today().isoformat()) > self.expires


@dataclass
class Profile:
    chat_title: str
    is_group: bool = False
    app: str = ""
    members: list[str] = field(default_factory=list)
    items: list[MemoryItem] = field(default_factory=list)
    analyses: int = 0
    updated: str = ""

    def brief(self, max_items: int = 14) -> dict:
        """给判断层用的精简视图：只给未过期的事实。

        群聊里额外按**人**分组输出 `about_people`——判断层需要知道"这句话关于谁"，
        否则会把不同成员当成一个模糊的"对方"（用户明确要求区分不同的人）。
        """
        today = date.today().isoformat()
        live = [i for i in self.items if not i.expired(today)]
        live.sort(key=lambda i: (i.kind, i.last_seen))
        by_kind: dict[str, list[str]] = {}
        by_person: dict[str, list[str]] = {}
        for i in live[:max_items]:
            by_kind.setdefault(i.kind, []).append(i.text)
            if i.about:
                by_person.setdefault(i.about, []).append(f"[{i.kind}] {i.text}")
        out = {
            "note": "以下是与本次对话相关的历史记忆，不是当前对话内容，仅供参考",
            "chat_title": self.chat_title,
            "is_group": self.is_group,
            "members": self.members[:12],
            "facts": by_kind,
        }
        if by_person:
            out["about_people"] = by_person
        return out


def slug(chat_title: str, app: str = "") -> str:
    h = hashlib.sha1(f"{app}|{chat_title}".encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", chat_title)[:40] or "chat"
    return f"{safe}_{h}"


def load(path: Path) -> Profile:
    if not path.exists():
        return Profile(chat_title=path.stem)
    d = json.loads(path.read_text(encoding="utf-8"))
    items = [MemoryItem(**it) for it in d.get("items", [])]
    return Profile(
        chat_title=d.get("chat_title", path.stem),
        is_group=bool(d.get("is_group")),
        app=d.get("app", ""),
        members=list(d.get("members") or []),
        items=items,
        analyses=int(d.get("analyses", 0)),
        updated=d.get("updated", ""),
    )


def save(prof: Profile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prof.updated = date.today().isoformat()
    payload = asdict(prof)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert(prof: Profile, new_items: list[MemoryItem]) -> tuple[int, int]:
    """按 key 覆盖更新。返回 (新增, 更新)。"""
    index = {i.key: i for i in prof.items}
    added = updated = 0
    for it in new_items:
        old = index.get(it.key)
        if old is None:
            if not it.first_seen:
                it.first_seen = date.today().isoformat()
            it.last_seen = it.last_seen or date.today().isoformat()
            prof.items.append(it)
            index[it.key] = it
            added += 1
        else:
            old.text = it.text or old.text
            old.evidence = it.evidence or old.evidence
            old.last_seen = date.today().isoformat()
            old.confidence = max(old.confidence, it.confidence)
            if it.expires:
                old.expires = it.expires
            updated += 1
    return added, updated


# ---------------------------------------------------------------- 蒸馏

DISTILL_PROMPT = """你在维护一份关于「某个聊天对象或群」的长期记忆档案。输入是这段聊天里最近的对话，
你要从中提取**值得长期记住的事实**，输出 JSON。

只提取这五类：
- identity：这位（或这些）说话人是谁、和手机主人是什么关系、扮演什么角色
- fact：关于对方或这个群的稳定事实（在做的事、项目、明确说过的情况）
- commitment：谁答应过做什么，或有什么待办（**必须给 expires**，估计一个合理到期日）
- preference：对方的偏好、习惯、忌讳
- boundary：明确不能碰的话题或做法

**绝对不要提的内容：**
- 推断或情绪解读（"他很在意""她在生气"）。只写可核对的事实。
- 一次性的闲聊、寒暄、表情包。
- 任何你不确定的内容。宁可少写，也不要写猜的。

每一条都必须带 evidence：从对话里摘一句原文当出处。没有原文支撑就不许输出这条。
每一条还要写 about：这条记忆**关于谁**（群里的昵称；关于整个群就写 null）。
来源（谁说的）写在 source_sender，不要和 about 混。

输出 JSON，不要解释，不要 markdown 围栏：
{
  "chat_title": "会话或群名称",
  "is_group": true,
  "members": ["出现在对话里的发言人昵称"],
  "items": [
    {"key": "稳定且简短的标识，如 wechat_id_blake_role",
     "kind": "identity|fact|commitment|preference|boundary",
     "about": "这条关于谁（群里昵称），关于整个群就 null",
     "text": "一句话中文事实",
     "evidence": "对话里的原文片段",
     "source_sender": "谁说的，没有就 null",
     "expires": "YYYY-MM-DD 或 null",
     "confidence": 0.0~1.0}
  ]
}"""


def distill(
    chat_title: str,
    messages: list,
    existing: Profile | None = None,
    provider: str | None = None,
    timeout: float = 180,
) -> tuple[Profile, dict]:
    """把一段对话蒸馏成记忆条目。已存在的 key 会被覆盖更新。

    messages 接受 [{"side","text","sender"}] 或 (side, text) 两种形态。
    带上 sender 能让蒸馏结果按人归档——群聊里这是必须的。
    """
    sys.path.insert(0, str(ROOT))
    from providers import chat as provider_chat  # noqa: E402

    def _line(m) -> str:
        if isinstance(m, dict):
            who = "我" if m.get("side") == "me" else (m.get("sender") or "对方")
            return f"{who}：{m.get('text', '')}"
        return f"{'我' if m[0] == 'me' else '对方'}：{m[1]}"

    convo = "\n".join(_line(m) for m in messages[-20:])
    known = ""
    if existing and existing.items:
        known = "\n已知（不要重复提取，除非有新信息）：\n" + "\n".join(
            f"- [{i.kind}] {i.text}" for i in existing.items[:20]
        )
    user = f"会话名称：{chat_title}\n\n最近对话：\n{convo}{known}\n\n请提取值得长期记住的事实。"

    t0 = time.time()
    content, usage, pinfo = provider_chat(
        [
            {"role": "system", "content": DISTILL_PROMPT},
            {"role": "user", "content": user},
        ],
        role="analysis",
        provider_override=provider,
        temperature=0,
    )
    elapsed = time.time() - t0

    s = re.sub(r"^```(?:json)?\s*", "", (content or "").strip())
    s = re.sub(r"\s*```$", "", s)
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise SystemExit(f"蒸馏返回的不是合法 JSON：{content[:400]}")
    data = json.loads(s[a : b + 1])

    prof = existing or Profile(chat_title=chat_title)
    prof.chat_title = data.get("chat_title") or chat_title
    if data.get("is_group") is not None:
        prof.is_group = bool(data.get("is_group"))
    for m in data.get("members") or []:
        if m and m not in prof.members:
            prof.members.append(str(m))

    items: list[MemoryItem] = []
    for it in data.get("items") or []:
        kind = str(it.get("kind") or "").strip()
        text = str(it.get("text") or "").strip()
        ev = str(it.get("evidence") or "").strip()
        key = str(it.get("key") or "").strip()
        if kind not in KINDS or not text or not ev or not key:
            continue  # 硬规矩 1：没有出处的丢弃
        items.append(
            MemoryItem(
                key=key,
                kind=kind,
                text=text,
                evidence=ev,
                source_chat=prof.chat_title,
                source_sender=it.get("source_sender"),
                about=it.get("about") or None,
                expires=it.get("expires") or None,
                confidence=float(it.get("confidence") or 0.7),
            )
        )

    added, updated = upsert(prof, items)
    prof.analyses += 1
    return prof, {
        "added": added,
        "updated": updated,
        "kept": len(items),
        "dropped": len(data.get("items") or []) - len(items),
        "elapsed_s": round(elapsed, 2),
        "usage": usage,
        "provider": pinfo,
    }


# ---------------------------------------------------------------- CLI


def _dir(p: str | None) -> Path:
    return Path(p) if p else DEFAULT_DIR


def main() -> int:
    ap = argparse.ArgumentParser(description="群组/联系人长期记忆档案")
    ap.add_argument("--dir", help="档案目录")
    ap.add_argument("--list", action="store_true", help="列出所有档案")
    ap.add_argument("--show", help="显示某个档案（按会话名匹配）")
    ap.add_argument("--distill", help="从 pipeline 的 JSON 结果蒸馏记忆")
    ap.add_argument("--from-export",
                    help="从 import_app_records.py 的 --distill-json 输出批量蒸馏（每会话一份档案）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出将要处理的会话与现有档案，不调用模型（用于核对流程而非花额度）")
    ap.add_argument("--provider", help="覆盖 roles.analysis 的 provider")
    args = ap.parse_args()

    d = _dir(args.dir)
    if args.list:
        files = sorted(d.glob("*.json"))
        if not files:
            print(f"({d} 下还没有档案)")
            return 0
        for f in files:
            p = load(f)
            today = date.today().isoformat()
            live = sum(1 for i in p.items if not i.expired(today))
            print(f"  {p.chat_title:<32} 群聊={str(p.is_group):<5} 记忆 {live}/{len(p.items)} 条  分析 {p.analyses} 次  更新 {p.updated}")
        return 0

    if args.show:
        files = sorted(d.glob("*.json"))
        hit = [f for f in files if args.show in load(f).chat_title or args.show in f.name]
        if not hit:
            print(f"没找到匹配 {args.show!r} 的档案")
            return 1
        p = load(hit[0])
        print(f"档案: {p.chat_title}   群聊={p.is_group}   成员={p.members}")
        print(f"分析 {p.analyses} 次   更新 {p.updated}")
        today = date.today().isoformat()
        for i in p.items:
            flag = "（已过期）" if i.expired(today) else ""
            print(f"\n  [{i.kind}] {i.text}{flag}")
            print(f"      key={i.key}  出处={i.source_sender or '-'}  {i.last_seen}  conf={i.confidence}")
            print(f"      原文: {i.evidence[:80]}")
        return 0

    if args.from_export:
        data = json.loads(Path(args.from_export).read_text(encoding="utf-8"))
        sessions = data.get("sessions") or []
        if not sessions:
            print("输入里没有 sessions")
            return 2
        print(f"待处理会话 {len(sessions)} 个" + ("（dry-run，不调用模型）" if args.dry_run else ""))
        for s in sessions:
            title = s.get("title") or "未知会话"
            msgs = s.get("messages") or []
            if not msgs:
                print(f"  - {title}: 没有消息，跳过")
                continue
            path = d / f"{slug(title)}.json"
            prof = load(path)
            if args.dry_run:
                preview = msgs[-2:]
                body = "；".join(f"{m.get('sender') or ('我' if m.get('side') == 'me' else '对方')}：{m.get('text')}"
                                for m in preview)
                print(f"  - {title}（群聊={s.get('is_group')}）{len(msgs)} 条消息"
                      f"  现有记忆 {len(prof.items)} 条  档案 {path.name}")
                print(f"      末尾两条：{body[:80]}")
                continue
            prof, meta = distill(title, msgs, prof, args.provider)
            if s.get("is_group") is not None:
                prof.is_group = bool(s.get("is_group"))
            save(prof, path)
            print(f"  - {title}: 新增 {meta['added']}  更新 {meta['updated']}  "
                  f"丢弃(无出处) {meta['dropped']}  耗时 {meta['elapsed_s']}s")

        if args.dry_run:
            # dry-run 是"核对流程"的终点，不是"没给参数"。跑到这里如果还落到下面的
            # print_help()，就会在成功输出后面再打一遍 usage —— 看起来像报错。
            print("\n（dry-run 结束，未调用模型、未写档案；去掉 --dry-run 才会真正蒸馏）")
            return 0

    if args.distill:
        src = Path(args.distill)
        data = json.loads(src.read_text(encoding="utf-8"))
        messages = [tuple(m) for m in data.get("messages") or []]
        title = (data.get("perception") or {}).get("title") or src.stem
        path = d / f"{slug(title)}.json"
        prof = load(path)
        prof, meta = distill(title, messages, prof, args.provider)
        save(prof, path)
        print(f"档案 {prof.chat_title}: 新增 {meta['added']}  更新 {meta['updated']}  "
              f"丢弃(无出处) {meta['dropped']}  耗时 {meta['elapsed_s']}s")
        print(f"已写 {path}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
