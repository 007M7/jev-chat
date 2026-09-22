import Foundation
import UIKit

/// 一次分析的完整结果（给界面和通知用）
struct JevAnalysis {
    var chatTitle: String
    var isGroup: Bool
    var speaker: String?            // 最新消息的发言人（群聊里有值）
    var contextLine: String?        // 同一发言人的上一条，做"上文"显示
    var latestText: String
    var intent: String              // 机器值，如 request_action
    var intentLabel: String         // 中文标签，如「要你办事」
    var intentConfidence: Double
    var danger: Double              // 0~9
    var dangerLabel: String         // 安全/留意/留神/危险/紧急
    var actionAdvice: String        // 一句话建议
    var shouldReplyNow: Double      // 0~1
    var candidates: [String]        // 已按 Jev 排序
    var consistencyWarnings: [String]
    var perceptionSeconds: Double
    var totalSeconds: Double
    var profileName: String
    var calibrated: Bool
    /// 这次实际注入给判断层的"关系前提"——显示出来便于核对模型到底以什么语境在判断
    var relationshipUsed: String
    /// 语境上的不确定处（标题没读到、群聊身份是推断的……），如实显示
    var contextNotes: [String]
}

enum JevError: LocalizedError {
    case config(String)
    case missingKey(String)
    case http(Int, String)
    case badJSON(String)

    var errorDescription: String? {
        switch self {
        case .config(let m): return "配置问题：\(m)"
        case .missingKey(let n): return "缺少密钥：请到设置里填 \(n)"
        case .http(let c, let m): return "HTTP \(c)：\(m)"
        case .badJSON(let m): return "返回格式不对：\(m)"
        }
    }
}

/// 端到端流水线：截图 → 感知 → 选题目档案 → 判断 → 起草 → 排序。
///
/// 每一步都刻意与 Python 原型 / 桌面版保持同一套语义：
///   感知 prompt 来自包内 prompts.json；题目来自包内 questions.json；
///   端点与模型来自包内 providers.json。这样三端不会各写一套后漂移。
final class JevPipeline {

    private let session: URLSession

    init() {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.waitsForConnectivity = true
        session = URLSession(configuration: cfg)
    }

    // MARK: 配置

    private func key(for envName: String) throws -> String {
        let v = KeychainStore.read(envName)
        if v.isEmpty { throw JevError.missingKey(envName) }
        return v
    }

    private func headers(for p: BrainConfig.Provider) throws -> [String: String] {
        var h = ["Content-Type": "application/json"]
        if !p.apiKeyEnv.isEmpty {
            let k = KeychainStore.read(p.apiKeyEnv)
            if k.isEmpty && !p.apiKeyOptional { throw JevError.missingKey(p.apiKeyEnv) }
            if !k.isEmpty { h["Authorization"] = "Bearer \(k)" }
        }
        for (k, v) in p.extraHeaders { h[k] = v }
        return h
    }

    // MARK: HTTP

    private func postJSON(_ body: [String: Any], to p: BrainConfig.Provider,
                          timeout: Double, retries: Int = 3) async throws -> [String: Any] {
        guard let url = URL(string: p.url) else { throw JevError.config("端点不合法: \(p.url)") }
        let h = try headers(for: p)
        let payload = try JSONSerialization.data(withJSONObject: body)
        var lastErr: Error?

        for attempt in 0...retries {
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.timeoutInterval = timeout
            for (k, v) in h { req.setValue(v, forHTTPHeaderField: k) }
            req.httpBody = payload
            do {
                let (data, resp) = try await session.data(for: req)
                let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if code == 200, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    return obj
                }
                let text = String(data: data, encoding: .utf8) ?? ""
                // 429/5xx 退避重试；其它错误直接抛出（客户端错误重试没意义）
                if (code == 429 || (500...599).contains(code)) && attempt < retries {
                    try? await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempt)) * 1_000_000_000))
                    lastErr = JevError.http(code, String(text.prefix(200)))
                    continue
                }
                throw JevError.http(code, String(text.prefix(300)))
            } catch let e as JevError {
                throw e
            } catch {
                lastErr = error
                if attempt < retries {
                    try? await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempt)) * 1_000_000_000))
                    continue
                }
            }
        }
        throw lastErr ?? JevError.http(0, "重试耗尽")
    }

    /// 容错解析：剥掉 markdown 围栏与前后杂字，取第一个 {...}
    private func parseJSONObject(_ content: String) throws -> [String: Any] {
        var s = content.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("```") {
            s = s.replacingOccurrences(of: "```json", with: "")
                .replacingOccurrences(of: "```", with: "")
        }
        if let a = s.firstIndex(of: "{"), let b = s.lastIndex(of: "}"), a < b,
           let obj = try? JSONSerialization.jsonObject(with: Data(s[a...b].utf8)) as? [String: Any] {
            return obj
        }
        throw JevError.badJSON(String(content.prefix(200)))
    }

    /// 与 JevClient.kt 的 parseThree 同策略：先取首个 [ 到末个 ]，失败再按行切
    private func parseThree(_ content: String) -> [String] {
        let s = content.trimmingCharacters(in: .whitespacesAndNewlines)
        if let a = s.firstIndex(of: "["), let b = s.lastIndex(of: "]"), a < b,
           let arr = try? JSONSerialization.jsonObject(with: Data(s[a...b].utf8)) as? [Any] {
            let out = arr.compactMap { ($0 as? String)?.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            if !out.isEmpty { return Array(out.prefix(3)) }
        }
        let prefixes = "-*0123456789.、) \"'"
        let lines = s.split(separator: "\n").map {
            String($0).trimmingCharacters(in: .whitespaces.union(CharacterSet(charactersIn: prefixes)))
        }.filter { !$0.isEmpty }
        return Array(lines.prefix(3))
    }

    // MARK: 中文标签

    private static let intentLabels: [String: String] = [
        "confirm_you_care": "在乎确认", "vent_anger": "情绪发泄", "request_action": "要你办事",
        "seek_explanation": "要个解释", "casual_chat": "闲聊", "close_topic": "话题收尾",
        "ask_info": "问信息", "ask_resource": "要资源", "share_news": "分享消息",
        "express_feeling": "表达情绪", "give_feedback": "给反馈",
    ]
    private static let actionLabels: [String: String] = [
        "check_history": "先翻聊天记录", "apologize": "先道歉", "give_commitment": "给具体承诺",
        "explain": "解释情况", "acknowledge": "表示听到了", "say_less": "少说两句",
        "make_plan": "约定具体安排",
        "answer_directly": "直接作答", "share_link": "给资源链接", "promise_and_follow": "承诺并跟进",
        "acknowledge_brief": "简短回应", "ask_clarify": "先问清楚", "take_private": "转为私聊",
        "no_reply": "不必回",
    ]
    /// 危险等级的中文标签与配色语义（与 docs/design/functional_spec.md 的映射表一致）
    static func dangerLabel(_ v: Double) -> String {
        switch v {
        case ..<2: return "安全"
        case ..<4: return "留意"
        case ..<6: return "留神"
        case ..<8: return "危险"
        default: return "紧急"
        }
    }

    /// 净化会话标题。
    ///
    /// 实测踩过：本 App 的通知横幅浮在微信上方时会被下一帧截图一起截进去，
    /// 标题于是被反复叠加——出现 `Jev · Jev · Jev · jev-chat-JARVIS 1 群`。
    /// 这会污染会话键（记录被拆成多个会话），所以只剥掉**开头连续的**通知前缀。
    /// 刻意只匹配开头的 `Jev ·` 形式：群里可能真叫 "jev-chat-JARVIS"，不能全局替换。
    static func sanitizeTitle(_ raw: String) -> String {
        var t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let prefixes = ["Jev · ", "Jev· ", "Jev ·", "Jev·", "Jev "]
        var changed = true
        while changed {
            changed = false
            for p in prefixes where t.hasPrefix(p) {
                t = String(t.dropFirst(p.count)).trimmingCharacters(in: .whitespaces)
                changed = true
            }
        }
        return t.isEmpty ? "未知会话" : t
    }

    /// 这条"最新消息"看起来是不是我们自己的输出（回声）。
    ///
    /// 通知横幅被截进画面时，模型可能把候选回复当成对方的新消息。
    /// 判定很严（归一化后完全相等或高度相似），避免误杀真实消息。
    static func looksLikeOurEcho(_ text: String, recentCandidates: [String]) -> Bool {
        func norm(_ s: String) -> String {
            s.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.union(.punctuationCharacters).contains($0) }
                .map(String.init).joined()
        }
        let n = norm(text)
        guard n.count >= 6 else { return false }        // 太短不比，避免误杀
        for c in recentCandidates {
            let m = norm(c)
            if m.isEmpty { continue }
            if n == m { return true }
            // 相似度过高也算（模型可能把 #1 前缀或个别字读错）
            if m.count >= 6, AnalysisStore.similarity(n, m) >= 0.9 { return true }
        }
        return false
    }

    // MARK: 主流程

    /// 会话档案的取用时机很关键：**必须在感知拿到标题之后**。
    /// 早期版本是调用方在分析前用"上一次的标题"猜档案——那会犯两个错：
    /// 第一次分析没有档案；切换会话时会把**上一个会话**的身份信息注入进来，
    /// 而注入错误的身份比不注入更糟（会稳定地把判断带偏）。
    typealias ProfileProvider = (_ title: String, _ isGroup: Bool)
        -> (relationship: String, memory: [String: Any]?)

    /// 上一帧已知的会话信息，用于兜住"这一帧读不到标题/判断不出群聊"的情况
    struct SessionHint {
        var title: String
        var isGroup: Bool
    }

    func analyze(image: UIImage,
                 sessionHint: SessionHint? = nil,
                 profileProvider: ProfileProvider? = nil) async throws -> JevAnalysis {
        let t0 = Date()

        // ---- 1) 感知 ----
        guard let perception = BrainConfig.provider(role: "perception") else {
            throw JevError.config("providers.json 里 roles.perception 没配好")
        }
        guard let jpeg = image.jpegData(compressionQuality: 0.8) else {
            throw JevError.config("当前帧无法编码为图片")
        }
        let dataURL = "data:image/jpeg;base64," + jpeg.base64EncodedString()
        let sys = BrainConfig.perceptionPrompt
        guard !sys.isEmpty else { throw JevError.config("prompts.json 里没有 perception.system_prompt") }

        let perceptionBody: [String: Any] = [
            "model": perception.model,
            "temperature": 0,
            "max_tokens": perception.maxTokens ?? 4000,
            "messages": [
                ["role": "system", "content": sys],
                ["role": "user", "content": [
                    ["type": "text", "text": BrainConfig.perceptionUserText],
                    ["type": "image_url", "image_url": ["url": dataURL]],
                ]],
            ],
        ]
        let pResp = try await postJSON(perceptionBody, to: perception, timeout: perception.timeout)
        let pChoices = pResp["choices"] as? [[String: Any]]
        let pContent = ((pChoices?.first?["message"] as? [String: Any])?["content"] as? String) ?? ""
        if pContent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw JevError.badJSON("感知返回空正文（思考型模型可能把 max_tokens 花在推理上）")
        }
        let structured = try parseJSONObject(pContent)
        let perceptionSeconds = Date().timeIntervalSince(t0)

        // 标题兜底：这一帧读不到标题时，用上一次已知的会话标题，
        // 否则会话键会变成「未知会话」，人工填的关系与人物档案就全部失效
        // （实测：标题被通知横幅盖住时正是这种情形）。
        let rawTitle = (structured["title"] as? String) ?? ""
        let cleaned = Self.sanitizeTitle(rawTitle)
        let titleUnknown = cleaned.isEmpty || cleaned == "未知会话"
        let chatTitle = titleUnknown ? (sessionHint?.title ?? "未知会话") : cleaned

        // **is_group 的兜底必须落到中性档案**：
        // 感知层没给出时（画面信息不足）如果落到 one_on_one，而它的默认关系描述是
        // 「对方是我的伴侣」——实测就在技术群里生成了恋人语气的话术。
        // "该亲密时没亲密"是轻错，"在工作群里凭空造出恋情"是重错，所以默认群聊档案。
        let explicitGroup = structured["is_group"] as? Bool
        let isGroup = explicitGroup ?? sessionHint?.isGroup ?? true
        let groupInferred = (explicitGroup == nil)
        let rawMsgs = (structured["messages"] as? [[String: Any]]) ?? []

        // 映射成统一形态：side / text / sender。非文本转方括号描述
        var msgs: [(side: String, text: String, sender: String?)] = []
        for m in rawMsgs {
            let side = ((m["side"] as? String) ?? "other") == "me" ? "me" : "other"
            let kind = (m["kind"] as? String) ?? "text"
            let text = (m["text"] as? String) ?? ""
            let sender = (m["sender"] as? String)?.trimmingCharacters(in: .whitespaces)
            var body = text
            if kind != "text" {
                let desc = (m["description"] as? String) ?? ""
                switch kind {
                case "sticker": body = desc.isEmpty ? "[表情包]" : "[表情包：\(desc)]"
                case "image":   body = desc.isEmpty ? "[图片]" : "[图片：\(desc)]"
                case "voice":   body = "[语音消息]"
                case "payment": body = desc.isEmpty ? "[红包/转账]" : "[\(desc)]"
                case "system":  continue
                default:        body = text.isEmpty ? "[\(kind)]" : text
                }
            }
            guard !body.isEmpty else { continue }
            msgs.append((side, body, (sender?.isEmpty ?? true) ? nil : sender))
        }
        let recent = Array(msgs.suffix(10))

        // ---- 2) 选题目档案 ----
        guard let qfile = BrainConfig.questionsFile,
              let profiles = qfile["profiles"] as? [String: Any] else {
            throw JevError.config("questions.json 里没有 profiles")
        }
        let profileName = isGroup ? "group" : "one_on_one"
        guard let profile = profiles[profileName] as? [String: Any],
              let questions = profile["judge_questions"] as? [String: Any] else {
            throw JevError.config("questions.json 里没有 profiles.\(profileName)")
        }
        let calibrated = (profile["calibrated"] as? Bool) ?? true
        let profileRelationship = (profile["relationship_default"] as? String) ?? ""
        // 人工填的会话档案优先于配置里的默认措辞——这是"关系前提不再靠猜"的落点
        // （题目集原本预设了亲密关系，群聊里根本没这层关系）。
        // 注意：档案在这里才取，因为此刻才拿到标题，能取到**本次会话**的档案。
        let ctx = profileProvider?(chatTitle, isGroup)
        let relationship = (ctx?.relationship.isEmpty == false) ? (ctx?.relationship ?? "") : profileRelationship
        let memory = ctx?.memory

        // ---- 3) 构造 state（群聊逐条带 sender）----
        var chatMsgs: [[String: Any]] = []
        for m in recent {
            var row: [String: Any] = ["from": m.side, "text": m.text]
            if m.side == "other", let s = m.sender { row["sender"] = s }
            chatMsgs.append(row)
        }
        var senders: [String] = []
        for m in recent where m.side == "other" {
            if let s = m.sender, !senders.contains(s) { senders.append(s) }
        }
        var chat: [String: Any] = [
            "relationship": relationship,
            "messages": chatMsgs,
            "latest_from": recent.last?.side ?? "other",
        ]
        if isGroup {
            chat["is_group"] = true
            if !senders.isEmpty {
                chat["senders"] = Array(senders.prefix(10))
                chat["distinct_speakers"] = senders.count
            }
        }
        // 人工维护的人物/会话档案作为"历史记忆"注入。与对话蒸馏出来的记忆不同，
        // 这是**人工权威值**，冲突时以它为准（见 docs/design/functional_spec.md）。
        if let memory { chat["memory"] = memory }
        let state: [String: Any] = ["chat": chat]

        // ---- 4) 判断 ----
        guard let judge = BrainConfig.provider(role: "judge") else {
            throw JevError.config("providers.json 里 roles.judge 没配好")
        }
        let judgeBody: [String: Any] = ["model": judge.model, "state": state, "questions": questions]
        let jResp = try await postJSON(judgeBody, to: judge, timeout: judge.timeout)
        // 答案可能在顶层 answers，也可能在 data.answers 下（两端历史实现都兼容）
        let answers = (jResp["answers"] as? [String: Any])
            ?? ((jResp["data"] as? [String: Any])?["answers"] as? [String: Any])
            ?? [:]

        func noul(_ k: String) -> Double { (answers[k] as? [String: Any])?["noul"] as? Double ?? 0 }
        func choice(_ k: String) -> String { (answers[k] as? [String: Any])?["choice"] as? String ?? "" }
        func confidence(_ k: String) -> Double { (answers[k] as? [String: Any])?["confidence"] as? Double ?? 0 }
        func score(_ k: String) -> Double {
            guard let a = answers[k] as? [String: Any] else { return 0 }
            if let probs = a["probabilities"] as? [String: Double], !probs.isEmpty {
                return probs.reduce(0.0) { acc, kv in acc + (Double(kv.key) ?? 0) * kv.value }
            }
            return (a["score"] as? Double) ?? 0
        }

        let intentKey = isGroup ? choice("asker_intent") : choice("true_intent")
        let dangerVal = isGroup ? score("group_tension") : score("danger_level")
        let actionKey = isGroup ? choice("best_group_action") : choice("best_action")
        let shouldReply = isGroup ? noul("need_reply") : noul("should_reply_now")

        // 最新一条与"上文"（同一发言人的前一条）
        let latest = recent.last
        let contextLine: String? = {
            guard let last = latest else { return nil }
            for m in recent.dropLast().reversed() where m.side == "other" && m.side == last.side {
                if last.sender == nil || m.sender == last.sender { return m.text }
            }
            return nil
        }()

        // ---- 5) 起草 ----
        guard let analysis = BrainConfig.provider(role: "analysis") else {
            throw JevError.config("providers.json 里 roles.analysis 没配好")
        }
        let draft = qfile["draft"] as? [String: Any] ?? [:]
        let selfLabel = (draft["self_label"] as? String) ?? "我"
        let otherLabel = (draft["other_label"] as? String) ?? "对方"
        let maxConvo = (draft["convo_messages"] as? Int) ?? 10
        let convo = recent.suffix(maxConvo).map { m -> String in
            let who = m.side == "me" ? selfLabel : (m.sender ?? otherLabel)
            return "\(who)：\(m.text)"
        }.joined(separator: "\n")
        let userText = ((draft["user_prompt_template"] as? String) ?? "关系：{relationship}\n\n最近对话：\n{convo}\n\n请给出 3 条候选回复。")
            .replacingOccurrences(of: "{relationship}", with: relationship)
            .replacingOccurrences(of: "{convo}", with: convo)

        // **把判断结论作为条件传给起草模型**。
        // 实测问题：Jev 判了「不必回」，候选却仍是"在呢，刚忙完，怎么啦？"这种等着接话的语气——
        // 因为起草模型只看到对话原文，不知道这条未必是冲我说的。
        // 这一步是"理解语境"的落点：结论必须影响候选的语气与多少。
        var draftUserText = userText
        // "不必由我回应"的判定：群聊看 best_group_action=no_reply，或 need_reply 低；
        // 一对一没有 no_reply 档，用 should_reply_now 低来近似
        let noReply = (actionKey == "no_reply") || (shouldReply < 0.4)
        var verdictLines: [String] = []
        verdictLines.append("已有判断（必须与之相符）：")
        verdictLines.append("- 发言人是：\(latest?.sender ?? "对方")")
        verdictLines.append("- 意图：\(Self.intentLabels[intentKey] ?? intentKey)")
        verdictLines.append("- 建议动作：\(Self.actionLabels[actionKey] ?? actionKey)")
        verdictLines.append(String(format: "- 是否该由我回应：%@（%.2f）",
                                   shouldReply >= 0.5 ? "是" : "否", shouldReply))
        if isGroup, let a = answers["asked_to_me"] as? [String: Any],
           let v = a["noul"] as? Double {
            verdictLines.append(String(format: "- 这条是否在对我说的：%@（%.2f）",
                                       v >= 0.5 ? "是" : "否", v))
        }
        if noReply {
            verdictLines.append("")
            verdictLines.append("因为判断是**不必由我回应**：三条候选都必须是**很轻的、不打断话题**的承接语"
                                + "（例如表示看到、稍后跟进、认同别人说的话），"
                                + "不要出现等待对方回答的提问，不要追问，不要主动挑起新话题。"
                                + "如果连承接都不必要，就把它们写得更短更随意。")
        }
        draftUserText += "\n\n" + verdictLines.joined(separator: "\n")

        var candidates: [String] = []
        do {
            let draftBody: [String: Any] = [
                "model": analysis.model,
                "temperature": (draft["temperature"] as? Double) ?? 0.8,
                "max_tokens": analysis.maxTokens ?? 2000,
                "messages": [
                    ["role": "system", "content": (draft["system_prompt"] as? String) ?? ""],
                    ["role": "user", "content": draftUserText],
                ],
            ]
            let dResp = try await postJSON(draftBody, to: analysis, timeout: analysis.timeout)
            let dContent = (((dResp["choices"] as? [[String: Any]])?.first?["message"] as? [String: Any])?["content"] as? String) ?? ""
            candidates = parseThree(dContent)
        } catch {
            // 起草失败不该让整次分析失败：判断结果仍然有价值
            candidates = []
        }
        while candidates.count < 3 { candidates.append("（稍等，我看下）") }

        // ---- 6) 排序 ----
        var ranked = candidates
        do {
            let rankQ: [String: Any] = [
                "best_reply": [
                    "type": "choice",
                    "instructions": ((profile["rank_question"] as? [String: Any])?["instructions"] as? String) ?? "",
                    "criteria": ["reply_a": candidates[0], "reply_b": candidates[1], "reply_c": candidates[2]],
                ]
            ]
            let rBody: [String: Any] = ["model": judge.model, "state": state, "questions": rankQ]
            let rResp = try await postJSON(rBody, to: judge, timeout: judge.timeout)
            let rAnswers = (rResp["answers"] as? [String: Any]) ?? [:]
            let probs = ((rAnswers["best_reply"] as? [String: Any])?["probabilities"] as? [String: Double]) ?? [:]
            let keys = ["reply_a", "reply_b", "reply_c"]
            var scored: [(String, Double)] = []
            for (i, k) in keys.enumerated() where i < candidates.count {
                scored.append((candidates[i], probs[k] ?? 0))
            }
            scored.sort { $0.1 > $1.1 }
            ranked = scored.map { $0.0 }
        } catch {
            // 排序失败就用原始顺序
        }

        // 语境的不确定处如实记下来，显示给用户——不假装确定
        var contextNotes: [String] = []
        if titleUnknown {
            contextNotes.append(sessionHint == nil
                ? "这一帧没读到会话标题，也无法沿用上一个会话"
                : "这一帧没读到会话标题，按上一个会话「\(sessionHint!.title)」处理")
        }
        if groupInferred {
            contextNotes.append("群聊身份是推断的（感知层没给出），已按中性档案判断")
        }

        return JevAnalysis(
            chatTitle: chatTitle,
            isGroup: isGroup,
            speaker: latest?.sender,
            contextLine: contextLine,
            latestText: latest?.text ?? "",
            intent: intentKey,
            intentLabel: Self.intentLabels[intentKey] ?? intentKey,
            intentConfidence: confidence(isGroup ? "asker_intent" : "true_intent"),
            danger: dangerVal,
            dangerLabel: Self.dangerLabel(dangerVal),
            actionAdvice: Self.actionLabels[actionKey] ?? actionKey,
            shouldReplyNow: shouldReply,
            candidates: ranked,
            consistencyWarnings: [],
            perceptionSeconds: perceptionSeconds,
            totalSeconds: Date().timeIntervalSince(t0),
            profileName: profileName,
            calibrated: calibrated,
            relationshipUsed: relationship,
            contextNotes: contextNotes
        )
    }
}
