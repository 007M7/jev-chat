"""群聊题目集。

为什么需要单独一套：`questions.py` 里那 7 道题是为一对一亲密/工作关系校准的
（`confirm_you_care`、`apologize`、`give_commitment` 这些档都预设了"对方在测试你在不在乎"）。
群聊里这层关系不存在，硬套就会把"对方在试探你"的读法塞进一个没有这层关系的场景。

实测证据（2026-09-21）：

| 数据 | 跨题矛盾 |
|---|---|
| 标注集 30 条（全是一对一关系场景） | 0 处 / 触发 50 次 = 0.0% |
| 真实群聊截图 A | 1 处：`true_intent=request_action` + `she_needs=action` 却 `should_reply_now=0.22` |
| 真实群聊截图 B | 0 处 |

即：题目集在一对一分布内是自洽的，出问题的是**场景外推**。而用户实际的截图全是群聊，
所以这是他最常撞到的缺陷。安卓版 README 也承认"群聊按一对一分析不准"，只是从没量化。

**这套题目尚未校准**：没有群聊标注集，命中率未知。用它得出的结论必须明确标注
「未经校准」，不能按一对一那套的可信度来用。要校准就需要用户先标注一批群聊对话
（20~30 条），再跑 `calibrate.py --questions group`。

口径与一对一那套保持一致：instructions / criteria 用英文（Jev 主训练语言是英文），
state 里的聊天内容保留中文原文。
"""

from __future__ import annotations

GROUP_QUESTIONS: dict = {
    "asked_to_me": {
        "type": "noul",
        "instructions": (
            "Is the latest message addressed to me, or does it need a reply from me? "
            "The conversation is a group: each message carries a sender, and the senders are "
            "different people. Check the sender of the latest message before answering. "
            "Answer FALSE if the speaker is talking to another member, replying to someone else's "
            "question, thinking out loud, sharing something for the group in general, or greeting "
            "the room without expecting me specifically. "
            "In a group chat most messages are not aimed at any one person."
        ),
        "criteria": {
            "true": (
                "The message names me, replies to something I said, asks a question only I would answer, "
                "or clearly expects my response as the project owner or the person being discussed."
            ),
            "false": (
                "The message is aimed at another named member, is a general remark to the room, "
                "is a greeting or a share, or is part of a back-and-forth between other people."
            ),
        },
    },
    "asker_intent": {
        "type": "choice",
        "instructions": (
            "What does the speaker actually want in the latest message? "
            "Judge from the whole thread, including what other members said before. "
            "A question about how something works is ask_info. "
            "Asking for people, resources, links, access, help, or collaboration is ask_resource. "
            "Sharing a result or news without asking anything is share_news. "
            "Do not read hidden relationship tests into a group chat."
        ),
        "criteria": {
            "ask_info": (
                "They want a fact, an answer, or an explanation of how something works. "
                "For example asking whether a signup bonus applies to every account, or what a limit is."
            ),
            "ask_resource": (
                "They want something concrete from me or from the group: people, links, invite codes, "
                "access, help, a deliverable, or a collaboration."
            ),
            "share_news": (
                "They are sharing a result, an announcement, or something they made, with no explicit request. "
                "A reply is optional rather than required."
            ),
            "express_feeling": (
                "They are mainly expressing a feeling: excitement, amusement, a joke, a complaint, "
                "or frustration. They are not asking for a fact or a deliverable."
            ),
            "give_feedback": (
                "They are commenting on something I or the group made: a suggestion, an improvement, "
                "or a problem report. The point is the product or the result, not a personal matter."
            ),
            "close_topic": (
                "They are wrapping up: a closing summary, a goodbye, 'that is all', "
                "or explicitly saying nothing more is needed."
            ),
        },
    },
    "group_tension": {
        "type": "score",
        "instructions": (
            "How much conflict or reputational risk is in this group conversation right now? "
            "Match the current scene. Light banter, reactions, and emoji are low. "
            "Public criticism of my project in front of a large group and a heated exchange "
            "between members are high, even when nothing is aimed at me."
        ),
        "criteria": [
            "Friendly banter, reactions, emoji, or neutral logistics. No friction at all.",
            "Mild teasing or a slightly awkward remark that is safe to ignore.",
            "A small complaint or a request implying mild dissatisfaction, said without heat.",
            "Noticeable dissatisfaction about something I made or arranged, still polite.",
            "Blunt but not hostile criticism, or a question implying I overpromised.",
            "Openly critical in front of the group; a careless reply could look defensive and cost trust.",
            "Complaint or blame directed at me publicly while other members watch how I handle it.",
            "Serious public criticism, or a demand for a correction or a refund.",
            "Hostile pile-on, or an escalation between members that I am expected to mediate.",
            "Active rupture in the group: insults, threats, mass exit, or a demand for accountability.",
        ],
    },
    "need_reply": {
        "type": "noul",
        "instructions": (
            "Does the latest message need a reply from me specifically, reasonably soon? "
            "Each message carries a sender, and the senders are different people; "
            "do not assume the latest speaker is the same person as the earlier one. "
            "Answer FALSE if it is aimed at another member, is a reply between two other people, "
            "is a share needing no response, is a closing remark, or if another member has already "
            "answered or fulfilled it. A direct question to me, a request for something only I have, "
            "or public criticism of my project is TRUE."
        ),
        "criteria": {
            "true": (
                "Someone is waiting on me: a direct question, a request only I can fulfil, "
                "public criticism of my work that calls for a response, or a closing summary "
                "that asks me to confirm."
            ),
            "false": (
                "No reply is needed from me: the message is aimed at another named member, "
                "is a reply between other people, is a share or a reaction, "
                "is something another member already handled, or the topic is already closed."
            ),
        },
    },
    "best_group_action": {
        "type": "choice",
        "instructions": (
            "What type of next action is best for me in this group? Choose only the action type; "
            "do not decide the exact wording. "
            "Check who has already replied: if another member already answered the question or "
            "provided the thing that was asked for, then no_reply or acknowledge_brief is right, "
            "not answering again. "
            "When people asked me for concrete resources such as links, codes, or collaborators, "
            "prefer share_link or promise_and_follow over a vague acknowledgement. "
            "When nothing was asked of me or the topic is closed, no_reply is a legitimate answer."
        ),
        "criteria": {
            "answer_directly": (
                "Give the fact or the answer in the group. Use when the question is clear "
                "and I actually know the answer."
            ),
            "share_link": (
                "Provide the concrete resource that was requested: a link, an invite code, "
                "a QR code, a document, or access."
            ),
            "promise_and_follow": (
                "Commit to an action and a time, then follow through. "
                "Use when the request is real but I cannot deliver it inside this message."
            ),
            "acknowledge_brief": (
                "A short warm reply: agreeing, thanking, or reacting. "
                "Use for banter, shares, and light remarks."
            ),
            "ask_clarify": (
                "Ask what exactly they mean or need before acting. "
                "Use when the request is ambiguous and guessing would waste effort."
            ),
            "take_private": (
                "Move it out of the group: suggest a private chat or a 1:1 follow-up. "
                "Use for money, complaints, personal matters, or anything that would embarrass someone publicly."
            ),
            "no_reply": (
                "Do not reply. Use when the message was not aimed at me, is a closing remark, "
                "or when adding words would only add noise."
            ),
        },
    },
    "need_from_me": {
        "type": "choice",
        "instructions": (
            "What does the speaker want from me right now, if anything? Judge the LATEST message first. "
            "If they already got what they wanted, or were only sharing, choose nothing. "
            "Friendly sarcasm or a joke is not a complaint. "
            "A request that another member already fulfilled needs nothing from me."
        ),
        "criteria": {
            "info": "They want a fact or an explanation from me and do not have it yet.",
            "resource": (
                "They want something concrete from me: people, links, codes, access, help, "
                "or collaboration, and have not received it yet."
            ),
            "recognition": (
                "They want me to acknowledge what they did or said: a contribution, a result they shared, "
                "or a concern they raised. They are not asking for a deliverable."
            ),
            "nothing": (
                "They need nothing from me: genuine sharing, friendly banter, a closed topic, "
                "or a request already fulfilled by me or by someone else."
            ),
        },
    },
    "topic_closed": {
        "type": "noul",
        "instructions": (
            "Can this topic be considered closed, so that I do not need to keep responding? "
            "Answer TRUE only if the thread reached a natural end: a summary was given, thanks were said, "
            "the chat went quiet, or the last message plainly expects no answer. "
            "An open question, a pending request, or an unaddressed complaint means FALSE."
        ),
        "criteria": {
            "true": (
                "The thread is at rest: a closing summary, a thank-you, a farewell, "
                "or a last message that plainly expects no answer."
            ),
            "false": (
                "Still open: a question waiting for an answer, a request not yet fulfilled, "
                "a complaint not yet addressed, or a discussion still in motion."
            ),
        },
    },
}

# 群聊里「对方」有多个发言人。把昵称列表带进 state，Jev 才知道这是群聊、
# 以及谁在说话——安卓版的一对一假设在这里不成立。
GROUP_RELATIONSHIP_DEFAULT = (
    "这是多人群聊，from=me 是我发的，from=other 是群成员发的（可能有多个人）。"
    "不要假设我和发言者之间有亲密关系，也不要按「对方在测试我在不在乎」解读。"
)


def build_group_state(messages: list, relationship: str = "", senders: list[str] | None = None) -> dict:
    """群聊专用的 state。

    与一对一 state 的关键差别：**每条消息带上 sender**，发言人不再被压成一个「对方」。
    判断层必须能看出"这个问题是 A 问的、B 已经回答了、C 在调侃"，
    否则它会以为所有话都是同一个人说的，也就无法判断"是不是已经有人处理了"。

    messages 接受两种形态：
      - dict：{"side": "me"|"other", "text": str, "sender": str|None}
      - tuple：(side, text)，无发言人信息
    这里用应用侧词表 me/other（不是一对一那套 her/me）。
    """
    cleaned = []
    for item in messages:
        if isinstance(item, dict):
            who = item.get("side") or item.get("from")
            text = item.get("text")
            sender = item.get("sender")
        else:
            who, text = item[0], item[1]
            sender = None
        if who in ("her", "other"):
            who = "other"
        if who not in ("me", "other"):
            raise ValueError(f"side must be 'me' or 'other', got {who!r}")
        row: dict = {"from": who, "text": str(text)}
        if who == "other" and sender:
            row["sender"] = str(sender)
        cleaned.append(row)
    cleaned = cleaned[-10:]

    if senders is None:
        seen: list[str] = []
        for r in cleaned:
            s = r.get("sender")
            if s and s not in seen:
                seen.append(s)
        senders = seen

    chat: dict = {
        "relationship": relationship or GROUP_RELATIONSHIP_DEFAULT,
        "is_group": True,
        "messages": cleaned,
        "latest_from": cleaned[-1]["from"] if cleaned else "other",
    }
    if senders:
        chat["senders"] = [s for s in senders if s][:10]
        # 只出现一个发言人时，实际更像一对一会话，明确写出来避免过度套用群聊假设
        chat["distinct_speakers"] = len(chat["senders"])
    return {"chat": chat}


def build_group_rank_question(candidates: list[str]) -> dict:
    """群聊版的排序题：强调公开场合要具体、别过度承诺。"""
    if len(candidates) != 3:
        raise ValueError("build_group_rank_question expects exactly 3 candidate replies")
    keys = ("reply_a", "reply_b", "reply_c")
    return {
        "best_reply": {
            "type": "choice",
            "instructions": (
                "Which candidate reply is the most appropriate next message in this group chat? "
                "Prefer the one that gives the requested fact or resource concretely, "
                "or that commits to a specific follow-up. "
                "Penalize vague acknowledgements when something concrete was asked for, "
                "public over-promising, and replies that add noise when nothing was asked of me."
            ),
            "criteria": {key: text for key, text in zip(keys, candidates)},
        }
    }
