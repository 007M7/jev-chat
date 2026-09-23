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
    /// 感知读到的消息条数。**0 表示这一帧不是可识别的聊天界面**（比如在桌面、别的 App、
    /// 或者在聊天列表页），调用方应据此跳过——否则会在非聊天界面反复产出无意义结果。
    var messageCount: Int
    /// 消息内容签名（对齐安卓 ChatModels.signature 的思路：取最后 6 条）。
    /// 用来做**内容级去重**：像素会因噪声微变，但"对话没变"就不该重复分析。
    var messageSignature: String
    /// 这次分析用到的对话原文（最近 10 条，已带发言人标签）。
    /// 导出到 PC 后用于：校准群聊题目集、蒸馏人物/会话记忆。
    /// 只存文本，不含截图。
    var transcript: [String]
    /// 同上，但保留结构。`transcript` 是它的文本形式，用于展示与导出；
    /// App 内部累积"群聊记录"用这一份，免得再从 `"发言人：内容"` 字符串反解——
    /// 那个往返在群聊里会把发言人认错（正是"没分清不同用户"的根源之一）。
    var lines: [ChatMsg]
    /// 七道题的**原始答案**（题名 → 简洁值，如 asked_to_me="0.11" / asker_intent="ask_resource"）。
    /// 只带中文标签的话没法按题目集做校准——校准需要逐题的机器值。
    var answersSummary: [String: String]
}

/// 一条在内存里流转的对话消息（感知层读出来的原始形态，还没有时间戳）。
///
/// 刻意用**具名结构体**而不是元组：之前这里同时存在
/// `(side, sender, text)` 和 `(side, text, sender)` 两种标签顺序，
/// 而 Swift 的 tuple shuffle 转换规则很微妙。本机没有 Swift 编译器，
/// 这类错只能等十分钟起步的云构建才发现，不如从类型上根除。
/// （落盘形态是 `AnalysisStore.ChatLine`，多一个时间戳。）
struct ChatMsg: Equatable {
    var side: String          // me | other
    var sender: String?       // 群聊里的发言人
    var text: String
}

enum JevError: LocalizedError {
    case config(String)
    case missingKey(String)
    case http(Int, String)
    case badJSON(String)
    /// 某个阶段超时。**必须单独一类**：用户看到的"一直转圈没结果"就是它，
    /// 而且要说清是哪一步超时，否则没法判断是网慢还是模型慢。
    case timeout(String, Double)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .config(let m): return "配置问题：\(m)"
        case .missingKey(let n): return "缺少密钥：请到设置里填 \(n)"
        case .http(let c, let m): return "HTTP \(c)：\(m)"
        case .badJSON(let m): return "返回格式不对：\(m)"
        case .timeout(let stage, let sec):
            return String(format: "%@ 超时（%.0f 秒内没返回）。已放弃这一轮，下一帧会自动重试。", stage, sec)
        case .cancelled: return "已取消"
        }
    }
}

/// 端到端流水线：截图 → 感知 → 选题目档案 → 判断 → 起草 → 排序。
///
/// 每一步都刻意与 Python 原型 / 桌面版保持同一套语义：
///   感知 prompt 来自包内 prompts.json；题目来自包内 questions.json；
///   端点与模型来自包内 providers.json。这样三端不会各写一套后漂移。
final class JevPipeline {

    /// 送进判断层的最大消息条数。
    /// 必须与 `tools/jev/questions.json` 的 `max_messages_in_state` 一致——
    /// 那边是 Python/桌面版共用的单一来源，改一处要改两处。
    static let maxMessagesInState = 10

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
        // 用 keyAccount 而不是 apiKeyEnv：用户自加的 provider 没有环境变量名，
        // 它的密钥账号是生成的。自带 provider 两者相同，老密钥不受影响。
        if !p.keyAccount.isEmpty {
            let k = KeychainStore.read(p.keyAccount)
            if k.isEmpty && !p.apiKeyOptional {
                throw JevError.missingKey("\(p.label)（\(p.keyAccount)）")
            }
            if !k.isEmpty { h["Authorization"] = "Bearer \(k)" }
        }
        for (k, v) in p.extraHeaders { h[k] = v }
        return h
    }

    // MARK: HTTP

    /// 发 POST 请求，**整个阶段受一个总预算约束**。
    ///
    /// 为什么必须有总预算：只设 `URLRequest.timeoutInterval` 是不够的——那是"多久没收到
    /// 数据"的空闲超时，不是总时长上限，而且它和重试是**相乘**关系。实测踩过：
    /// `retries: 3` 配上 provider 里 180 秒的 `timeout_default`，最坏是 4×180 秒再加退避
    /// ≈ 12 分钟。用户看到的就是"一直转圈、几分钟不出结果、也不弹通知"——
    /// 卡住期间所有帧都被丢掉。
    ///
    /// 做法：
    ///   · 每次尝试的超时 = **剩余预算**（所以单次尝试不可能超出总预算）；
    ///   · 重试前先看还剩多少，剩余不足 2 秒就直接抛超时，不再发起；
    ///   · 退避等待也不许吃掉预算。
    private func postJSON(_ body: [String: Any], to p: BrainConfig.Provider,
                          budget: Double, stage: String, retries: Int = 1) async throws -> [String: Any] {
        guard let url = URL(string: p.url) else { throw JevError.config("端点不合法: \(p.url)") }
        let h = try headers(for: p)
        let payload = try JSONSerialization.data(withJSONObject: body)
        let start = Date()
        var lastErr: Error?

        for attempt in 0...retries {
            let remaining = budget - Date().timeIntervalSince(start)
            guard remaining > 2 else {
                throw lastErr ?? JevError.timeout(stage, budget)
            }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.timeoutInterval = remaining
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
                    lastErr = JevError.http(code, String(text.prefix(200)))
                    let left = budget - Date().timeIntervalSince(start)
                    if left > 4 { try? await Task.sleep(nanoseconds: UInt64(min(pow(2.0, Double(attempt)), left - 2) * 1_000_000_000)) }
                    continue
                }
                throw JevError.http(code, String(text.prefix(300)))
            } catch let e as JevError {
                throw e
            } catch {
                // URLSession 的超时/断连走的这里。同一次 stage 内最多再试一次，
                // 且仍然受总预算约束——本地 4G 抖动时不会变成"重试到天荒地老"。
                lastErr = error
                if attempt < retries {
                    let left = budget - Date().timeIntervalSince(start)
                    if left > 4 { try? await Task.sleep(nanoseconds: UInt64(min(2.0, left - 2) * 1_000_000_000)) }
                    continue
                }
            }
        }
        // 循环结束还没成功：区分"没时间了"与"试完了"
        if Date().timeIntervalSince(start) >= budget - 2 { throw JevError.timeout(stage, budget) }
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
    /// 危险等级的中文标签与配色语义（与 Android 版的映射表一致，别各写一套）
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
    /// 感知拿到标题后向调用方要的「本次会话的已知前提」。
    /// 三样都是**感知之后才能取**的：关系描述、人工档案、以及累积的群聊上下文。
    /// 刻意不用元组——已经扩过一次（加 priorLines），再加字段时结构体更稳。
    struct SessionContext {
        var relationship: String = ""
        var memory: [String: Any]? = nil
        /// 跨帧累积的历史消息（不含这一帧读到的）。单帧只能看到当前屏，
        /// 而群聊里判断最新那条往往需要更早的上下文——用户明确指出过这一点。
        var priorLines: [ChatMsg] = []
    }

    typealias ContextProvider = (_ title: String, _ isGroup: Bool) -> SessionContext

    /// 上一帧已知的会话信息，用于兜住"这一帧读不到标题/判断不出群聊"的情况
    struct SessionHint {
        var title: String
        var isGroup: Bool
    }

    /// - Parameter skipDraft: 跳过起草候选与排序，只出判断。
    ///   实测起草+排序占总耗时的一半左右（感知 4~7s / 判断 1s / 起草 2~7s / 排序 1s），
    ///   所以"只想知道对方什么意思、不要候选"时能省掉一半时间。
    func analyze(image: UIImage,
                 sessionHint: SessionHint? = nil,
                 skipDraft: Bool = false,
                 contextProvider: ContextProvider? = nil,
                 onStage: ((String) -> Void)? = nil) async throws -> JevAnalysis {
        let t0 = Date()
        onStage?("感知中")

        // ---- 1) 感知 ----
        guard let perception = BrainConfig.provider(role: "perception") else {
            throw JevError.config("providers.json 里 roles.perception 没配好")
        }
        // **先把图缩小再上传**。
        // 屏幕截图是 1180×2556 左右，整张转 base64 有几百 KB；
        // 实测在网络不稳时（用户环境）感知一步能拖到 47 秒——上传体积是可控的那部分。
        // 视觉模型读聊天文字不需要原分辨率，长边压到 1600 足够，
        // 体积大约降到原来的 1/3。
        let prepared = Self.downscaled(image, maxSide: 1600)
        guard let jpeg = prepared.jpegData(compressionQuality: 0.7) else {
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
        // 预算按**实测耗时**给（感知最慢 15.5s），不是 provider 里那个通用超时。
        let pBudget = ProvidersStore.shared.timeout(for: "perception")
        let pResp = try await postJSON(perceptionBody, to: perception,
                                       budget: pBudget, stage: "感知", retries: 1)
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

        let rawMsgs = (structured["messages"] as? [[String: Any]]) ?? []

        // 映射成统一形态：side / text / sender。非文本转方括号描述
        var msgs: [ChatMsg] = []
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
            msgs.append(ChatMsg(side: side,
                                sender: (sender?.isEmpty ?? true) ? nil : sender,
                                text: body))
        }

        // **is_group 的兜底必须落到中性档案**：
        // 感知层没给出时（画面信息不足）如果落到 one_on_one，而它的默认关系描述是
        // 「对方是我的伴侣」——实测就在技术群里生成了恋人语气的话术。
        // "该亲密时没亲密"是轻错，"在工作群里凭空造出恋情"是重错，所以默认群聊档案。
        let explicitGroup = structured["is_group"] as? Bool

        // **交叉校验**：对方阵营出现两个以上不同发言人时，它不可能是单聊。
        // 实测踩过——256 人的群被判成 is_group=false，于是用了单聊档案、生成了恋人话术。
        // 这个判据完全来自数据本身，比模型的 is_group 字段更可靠。
        // 用**这一帧**的消息做判定：只需要帧内发言人，而累积上下文此刻还取不到
        // （它要等标题与 isGroup 定了才能按会话取）。
        var distinctOtherSenders: [String] = []
        for m in msgs where m.side == "other" {
            if let s = m.sender, !distinctOtherSenders.contains(s) { distinctOtherSenders.append(s) }
        }
        let groupBySenders = distinctOtherSenders.count >= 2

        let isGroup: Bool
        if groupBySenders {
            isGroup = true
        } else {
            isGroup = explicitGroup ?? sessionHint?.isGroup ?? true
        }
        let groupInferred = (explicitGroup == nil) || (explicitGroup == false && groupBySenders)

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
        let ctx = contextProvider?(chatTitle, isGroup)
        let relationship = (ctx?.relationship.isEmpty == false) ? (ctx?.relationship ?? "") : profileRelationship
        let memory = ctx?.memory
        let priorLines = ctx?.priorLines ?? []

        // **把跨帧累积的上下文与这一帧读到的合并**，再取最近 10 条交给判断层。
        // 为什么需要：单张截图只能看到当前屏的几条；如果某帧只读到两三条，
        // 判断层就缺上下文。合并后能补回更早的消息（用户明确指出群聊下需要上下文）。
        // 合并策略与 AnalysisStore.mergeConversation 同构：找尾部与开头的最大重合，去重后追加。
        let merged = Self.mergeContext(prior: priorLines, frame: msgs)
        let recent = Array(merged.suffix(Self.maxMessagesInState))

        // ---- 3) 构造 state（群聊逐条带 sender）----
        var chatMsgs: [[String: Any]] = []
        for m in recent {
            var row: [String: Any] = ["from": m.side, "text": m.text]
            if m.side == "other", let s = m.sender { row["sender"] = s }
            chatMsgs.append(row)
        }
        let senders = distinctOtherSenders
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
        // 这是**人工权威值**，冲突时以它为准：模型推断出来的只是候选。
        if let memory { chat["memory"] = memory }
        let state: [String: Any] = ["chat": chat]

        // ---- 4) 判断 ----
        guard let judge = BrainConfig.provider(role: "judge") else {
            throw JevError.config("providers.json 里 roles.judge 没配好")
        }
        let judgeBody: [String: Any] = ["model": judge.model, "state": state, "questions": questions]
        let jBudget = ProvidersStore.shared.timeout(for: "judge")
        onStage?("判断中")
        let jResp = try await postJSON(judgeBody, to: judge,
                                       budget: jBudget, stage: "判断", retries: 2)
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
        if !skipDraft {
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
            let dBudget = ProvidersStore.shared.timeout(for: "analysis")
            onStage?("起草候选")
            let dResp = try await postJSON(draftBody, to: analysis,
                                           budget: dBudget, stage: "起草", retries: 1)
            let dContent = (((dResp["choices"] as? [[String: Any]])?.first?["message"] as? [String: Any])?["content"] as? String) ?? ""
            candidates = parseThree(dContent)
        } catch {
            // 起草失败不该让整次分析失败：判断结果仍然有价值
            candidates = []
        }
        }
        if !skipDraft {
            while candidates.count < 3 { candidates.append("（稍等，我看下）") }
        }

        // ---- 6) 排序 ----
        var ranked = candidates
        if !skipDraft {
        do {
            let rankQ: [String: Any] = [
                "best_reply": [
                    "type": "choice",
                    "instructions": ((profile["rank_question"] as? [String: Any])?["instructions"] as? String) ?? "",
                    "criteria": ["reply_a": candidates[0], "reply_b": candidates[1], "reply_c": candidates[2]],
                ]
            ]
            let rBody: [String: Any] = ["model": judge.model, "state": state, "questions": rankQ]
            onStage?("排序候选")
            let rResp = try await postJSON(rBody, to: judge,
                                           budget: ProvidersStore.shared.timeout(for: "rank"),
                                           stage: "排序", retries: 2)
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
        }

        // 语境的不确定处如实记下来，显示给用户——不假装确定
        var contextNotes: [String] = []
        if titleUnknown {
            contextNotes.append(sessionHint == nil
                ? "这一帧没读到会话标题，也无法沿用上一个会话"
                : "这一帧没读到会话标题，按上一个会话「\(sessionHint!.title)」处理")
        }
        if groupInferred {
            contextNotes.append(groupBySenders
                ? "对方阵营有 \(distinctOtherSenders.count) 个发言人，据此判定为群聊（覆盖了感知层的判断）"
                : "群聊身份是推断的（感知层没给出），已按中性档案判断")
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
            contextNotes: contextNotes,
            messageCount: recent.count,
            messageSignature: Self.signature(of: recent),
            transcript: recent.map { m in
                let who = m.side == "me" ? "我" : (m.sender ?? "对方")
                return "\(who)：\(m.text)"
            },
            lines: recent,
            answersSummary: questions.keys.reduce(into: [String: String]()) { acc, k in
                guard let a = answers[k] as? [String: Any] else { return }
                if let v = a["noul"] as? Double { acc[k] = String(format: "%.2f", v) }
                else if let c = a["choice"] as? String { acc[k] = c }
                else if let s = a["score"] as? Double { acc[k] = String(format: "%.2f", s) }
            }
        )
    }

    /// 把"累积的上下文"与"这一帧读到的"合并：找尾部与开头的最大重合，去掉重合再追加。
    /// 逐帧前进时不会重复；用户往回翻则被去重。
    static func mergeContext(prior: [ChatMsg], frame: [ChatMsg]) -> [ChatMsg] {
        guard !prior.isEmpty else { return frame }
        let maxK = min(prior.count, frame.count)
        var overlap = 0
        if maxK > 0 {
            // 从最长的可能重合往回试：重叠越多说明这一帧越靠后，越该以它为准
            for k in stride(from: maxK, through: 1, by: -1) {
                if Array(prior.suffix(k)) == Array(frame.prefix(k)) { overlap = k; break }
            }
        }
        var out = Array(prior.dropLast(overlap))
        out.append(contentsOf: frame)
        // 整体去重（保留首次出现），预防回翻造成的重复
        var seen = Set<String>()
        var deduped: [ChatMsg] = []
        for m in out {
            let key = "\(m.side)|\(m.sender ?? "")|\(m.text)"
            if seen.insert(key).inserted { deduped.append(m) }
        }
        return deduped
    }

    /// 长边压到 maxSide。用于降低感知请求的上传体积。
    static func downscaled(_ image: UIImage, maxSide: CGFloat) -> UIImage {
        let w = image.size.width, h = image.size.height
        guard w > 0, h > 0 else { return image }
        let scale = maxSide / max(w, h)
        if scale >= 1 { return image }
        let target = CGSize(width: (w * scale).rounded(), height: (h * scale).rounded())
        let renderer = UIGraphicsImageRenderer(size: target)
        return renderer.image { _ in image.draw(in: CGRect(origin: .zero, size: target)) }
    }

    /// 消息内容签名：取最后 8 条（与安卓 ChatModels.signature 同思路），**用于内容级去重**。
    ///
    /// 关键细节：**非文本消息只按类型入签名，不带描述**。
    /// 实测踩过——用户在不同界面翻阅不同图片时，图片描述每次都不同，于是签名每次都变、
    /// 去重失效，同一句「慢 有办法解决没」被反复分析了三次（19:54/55/56）。
    /// 改用稳定的 `[图片]` / `[表情包]` 占位后，这类噪声不会再触发重复分析。
    static func signature(of msgs: [ChatMsg]) -> String {
        func stable(_ text: String) -> String {
            guard text.hasPrefix("["), let close = text.firstIndex(of: "]") else { return text }
            let inner = text[text.index(after: text.startIndex)..<close]
            let kind = inner.split(separator: "：").first.map(String.init) ?? String(inner)
            return "[\(kind)]"
        }
        return msgs.suffix(8).map { "\($0.side):\(stable($0.text))" }.joined(separator: "|")
    }
}
