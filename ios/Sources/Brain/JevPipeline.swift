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

    // MARK: 主流程

    func analyze(image: UIImage) async throws -> JevAnalysis {
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

        let chatTitle = (structured["title"] as? String) ?? "未知会话"
        let isGroup = (structured["is_group"] as? Bool) ?? false
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
        let relationship = (profile["relationship_default"] as? String) ?? ""

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

        var candidates: [String] = []
        do {
            let draftBody: [String: Any] = [
                "model": analysis.model,
                "temperature": (draft["temperature"] as? Double) ?? 0.8,
                "max_tokens": analysis.maxTokens ?? 2000,
                "messages": [
                    ["role": "system", "content": (draft["system_prompt"] as? String) ?? ""],
                    ["role": "user", "content": userText],
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
            calibrated: calibrated
        )
    }
}
