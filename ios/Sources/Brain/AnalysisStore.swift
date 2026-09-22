import Foundation

/// 一条被记录下来的分析（App 内的"对话记录"）
struct StoredAnalysis: Codable, Identifiable {
    var id: String = UUID().uuidString
    var at: Date = Date()
    var sessionKey: String
    var chatTitle: String
    var isGroup: Bool
    var speaker: String?            // 最新消息的发言人
    var latestText: String
    var contextLine: String?
    var danger: Double
    var dangerLabel: String
    var intentLabel: String
    var intentConfidence: Double
    var actionAdvice: String
    var candidates: [String]
    /// 用户点了第几个候选（0 基）。这是"建议好不好"的唯一客观真值，将来做校准要用。
    var pickedIndex: Int?
    /// 这次实际注入的关系前提（便于核对模型以什么语境在判断）
    var relationshipUsed: String = ""
    /// 语境的不确定处（标题没读到、群聊身份是推断的……）
    var contextNotes: [String] = []
    var profileName: String = ""
    var calibrated: Bool = true
}

/// 会话档案：**人工填的身份信息**，注入判断层。
///
/// 这是本方案里提升准确度最直接的一环：题目集原本要"猜"关系前提
/// （一对一那套预设了"对方在测试你在不在乎"，群聊里根本没这层关系）。
/// 人工把"这个群是什么、这个人是谁"写清楚，判断层就不用猜了。
///
/// 与 memory 的关系（见 docs/design/functional_spec.md）：
/// 这里是**人工权威值**，将来由对话蒸馏出来的记忆是**候选值**，冲突时以人工为准。
struct SessionProfile: Codable {
    var sessionKey: String
    var title: String
    var isGroup: Bool
    /// key = 发言人昵称，或 "group"（整个会话/群本身的定位）；value = 身份描述
    var notes: [String: String] = [:]
    /// 关系描述（会替代 questions.json 里的默认措辞）
    var relationship: String = ""
    var updatedAt: Date = Date()

    /// 拼成给判断层的 relationship 文本
    func relationshipText(fallback: String) -> String {
        var parts: [String] = []
        if !relationship.isEmpty { parts.append(relationship) }
        if let g = notes["group"], !g.isEmpty { parts.append("会话定位：\(g)") }
        let people = notes.filter { $0.key != "group" && !$0.value.isEmpty }
        if !people.isEmpty {
            let list = people.map { "\($0.key)：\($0.value)" }.joined(separator: "；")
            parts.append("已知成员：\(list)")
        }
        return parts.isEmpty ? fallback : parts.joined(separator: "。")
    }
}

/// 本地记录：按会话分组保存分析结果，并维护会话档案。
///
/// 存 Documents 目录（App 私有），**不上传**。截图本身仍只留在内存里，
/// 这里存的是结构化文本（对话摘录 + 判断结果），是"可查的记录"而非原始图像。
final class AnalysisStore: ObservableObject {

    static let shared = AnalysisStore()

    @Published private(set) var analyses: [StoredAnalysis] = []
    @Published private(set) var profiles: [String: SessionProfile] = [:]

    private let historyURL = AnalysisStore.docURL("jev_history.json")
    private let profileURL = AnalysisStore.docURL("jev_profiles.json")

    private static func docURL(_ name: String) -> URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(name)
    }

    init() { load() }

    // MARK: 会话键
    //
    // 视觉模型会把标题读错（桌面版实测同一张图读出过两个字不同的标题），
    // 所以会话键用**归一化 + 相似度**匹配，不能逐字比——否则每次误读都被当成"切换了会话"。
    static func normalize(_ title: String) -> String {
        let drop = CharacterSet(charactersIn: " \u{3000}📌✨⭐️").union(.symbols)
        return title.unicodeScalars.filter { !drop.contains($0) }.map(String.init).joined()
    }

    /// 找到与给定标题最匹配的已有会话键
    func sessionKey(for title: String, isGroup: Bool) -> String {
        let n = Self.normalize(title)
        if let exact = profiles.values.first(where: { Self.normalize($0.title) == n }) {
            return exact.sessionKey
        }
        var best: (String, Double)?
        for p in profiles.values {
            let r = Self.similarity(Self.normalize(p.title), n)
            if r >= 0.6, r > (best?.1 ?? 0) { best = (p.sessionKey, r) }
        }
        if let b = best { return b.0 }
        // 新会话：用归一化标题当键（比 hash 可读，便于人工核对）
        return "\(isGroup ? "group" : "dm"):\(n)"
    }

    static func similarity(_ a: String, _ b: String) -> Double {
        if a.isEmpty || b.isEmpty { return 0 }
        if a == b { return 1 }
        let sa = Array(a), sb = Array(b)
        var common = 0
        var i = 0, j = 0
        // 简化版最长公共子序列长度（标题很短，代价可忽略）
        var dp = [[Int]](repeating: [Int](repeating: 0, count: sb.count + 1), count: sa.count + 1)
        for x in 1...sa.count {
            for y in 1...sb.count {
                dp[x][y] = sa[x-1] == sb[y-1] ? dp[x-1][y-1] + 1 : max(dp[x-1][y], dp[x][y-1])
            }
        }
        common = dp[sa.count][sb.count]
        _ = (i, j)
        return Double(common) / Double(max(sa.count, sb.count))
    }

    // MARK: 记录

    func add(_ a: StoredAnalysis) {
        analyses.append(a)
        // 只留最近 500 条，避免文件无限增长
        if analyses.count > 500 { analyses.removeFirst(analyses.count - 500) }
        ensureProfile(key: a.sessionKey, title: a.chatTitle, isGroup: a.isGroup)
        save()
    }

    func markPicked(analysisID: String, index: Int) {
        guard let i = analyses.firstIndex(where: { $0.id == analysisID }) else { return }
        analyses[i].pickedIndex = index
        save()
    }

    /// 按会话分组（最近活跃在前）
    var sessions: [(key: String, title: String, isGroup: Bool, count: Int, lastAt: Date)] {
        var byKey: [String: (String, Bool, Int, Date)] = [:]
        for a in analyses {
            if var cur = byKey[a.sessionKey] {
                cur.2 += 1
                if a.at > cur.3 { cur.3 = a.at }
                byKey[a.sessionKey] = cur
            } else {
                byKey[a.sessionKey] = (a.chatTitle, a.isGroup, 1, a.at)
            }
        }
        return byKey.map { (key: $0.key, title: $0.value.0, isGroup: $0.value.1,
                            count: $0.value.2, lastAt: $0.value.3) }
            .sorted { $0.lastAt > $1.lastAt }
    }

    func analyses(in sessionKey: String) -> [StoredAnalysis] {
        analyses.filter { $0.sessionKey == sessionKey }.sorted { $0.at > $1.at }
    }

    /// 某会话里出现过的发言人
    func speakers(in sessionKey: String) -> [String] {
        var seen: [String] = []
        for a in analyses where a.sessionKey == sessionKey {
            if let s = a.speaker, !seen.contains(s) { seen.append(s) }
        }
        return seen
    }

    // MARK: 档案

    func ensureProfile(key: String, title: String, isGroup: Bool) {
        if profiles[key] == nil {
            profiles[key] = SessionProfile(sessionKey: key, title: title, isGroup: isGroup)
        } else {
            profiles[key]?.title = title
            profiles[key]?.isGroup = isGroup
        }
    }

    func setNote(_ text: String, person: String, key: String) {
        guard var p = profiles[key] else { return }
        if text.trimmingCharacters(in: .whitespaces).isEmpty {
            p.notes.removeValue(forKey: person)
        } else {
            p.notes[person] = text
        }
        p.updatedAt = Date()
        profiles[key] = p
        save()
    }

    func setRelationship(_ text: String, key: String) {
        guard var p = profiles[key] else { return }
        p.relationship = text
        p.updatedAt = Date()
        profiles[key] = p
        save()
    }

    func clearAll() {
        analyses.removeAll()
        profiles.removeAll()
        save()
    }

    // MARK: 落盘

    private func load() {
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .iso8601
        if let d = try? Data(contentsOf: historyURL),
           let v = try? dec.decode([StoredAnalysis].self, from: d) { analyses = v }
        if let d = try? Data(contentsOf: profileURL),
           let v = try? dec.decode([String: SessionProfile].self, from: d) { profiles = v }
    }

    private func save() {
        let enc = JSONEncoder()
        enc.dateEncodingStrategy = .iso8601
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let d = try? enc.encode(analyses) { try? d.write(to: historyURL, options: .atomic) }
        if let d = try? enc.encode(profiles) { try? d.write(to: profileURL, options: .atomic) }
    }
}
