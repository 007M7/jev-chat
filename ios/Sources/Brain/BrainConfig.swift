import Foundation
import Security

/// 配置读取：从 App 包内读那份**跨端单一来源**的 JSON，密钥从 Keychain 取。
///
/// 为什么读包内 JSON 而不是在 Swift 里再抄一份：
/// `tools/jev/questions.json`（题目）、`prompts.json`（感知 prompt）、`providers.json`（端点与模型）
/// 同时被 Python 原型、桌面版、iOS 三处使用。手抄第三份必然漂移，
/// 所以由构建时把它们打进包内，运行时读同一份文件。校验由 `tools/jev/check_questions.py`
/// 与 `check_prompts.py` 负责。
enum BrainConfig {

    /// 读包内的 JSON 文件
    static func loadJSON(_ name: String) -> [String: Any]? {
        guard let url = Bundle.main.url(forResource: name, withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return obj
    }

    /// providers.json 里的一个 provider
    struct Provider {
        let id: String
        let kind: String            // systemone | openai_chat
        let baseURL: String
        let path: String
        let model: String
        let apiKeyEnv: String
        let apiKeyOptional: Bool
        let maxTokens: Int?
        let timeout: Double
        let extraHeaders: [String: String]

        var url: String { baseURL.hasSuffix("/") ? String(baseURL.dropLast()) + path : baseURL + path }
    }

    /// 按角色取 provider。roles 里存的只是 provider id，真正的端点/模型在 providers 里。
    static func provider(role: String, override: String? = nil) -> Provider? {
        guard let cfg = loadJSON("providers"),
              let roles = cfg["roles"] as? [String: Any],
              let providers = cfg["providers"] as? [String: Any] else { return nil }
        let pid = override ?? (roles[role] as? String)
        guard let id = pid, let p = providers[id] as? [String: Any] else { return nil }
        return Provider(
            id: id,
            kind: (p["kind"] as? String) ?? "",
            baseURL: (p["base_url"] as? String) ?? "",
            path: (p["path"] as? String) ?? "",
            model: (p["model"] as? String) ?? "",
            apiKeyEnv: (p["api_key_env"] as? String) ?? "",
            apiKeyOptional: (p["api_key_optional"] as? Bool) ?? false,
            maxTokens: p["max_tokens"] as? Int,
            timeout: (p["timeout_default"] as? Double) ?? 60,
            extraHeaders: (p["extra_headers"] as? [String: String]) ?? [:]
        )
    }

    /// 设置页要显示的密钥字段 = **当前被角色实际用到的 provider 所需的环境变量**。
    ///
    /// 只列三个角色（judge / analysis / perception）指向的 provider，
    /// 不把配置里存在但没启用的 provider（例如备用的 openrouter_*）也列出来——
    /// 否则用户会看到用不到的输入框，不知道该填哪个。
    ///
    /// 坑（实测踩过）：`api_key_optional` **缺失时必须当作 false（必需）**。
    /// 曾经写成 `p["api_key_optional"] as? Bool, !optional`，没有该字段的 provider
    /// 会因为 `as? Bool` 返回 nil 而被整个跳过，七个 provider 全被跳光，
    /// 设置页一个输入框都不显示。
    static func allKeyNames() -> [String] {
        guard let cfg = loadJSON("providers"),
              let providers = cfg["providers"] as? [String: Any] else { return [] }

        func envName(ofProvider id: String) -> String? {
            guard let p = providers[id] as? [String: Any],
                  let name = p["api_key_env"] as? String, !name.isEmpty else { return nil }
            let isOptional = (p["api_key_optional"] as? Bool) ?? false
            return isOptional ? nil : name
        }

        var names: [String] = []
        let roles = (cfg["roles"] as? [String: Any]) ?? [:]
        for role in ["judge", "analysis", "perception"] {
            guard let pid = roles[role] as? String, let n = envName(ofProvider: pid) else { continue }
            if !names.contains(n) { names.append(n) }
        }

        // 兜底：roles 没配全时，退回到"所有非可选 provider 的密钥"，
        // 保证设置页永远不会空着（空着用户就没法填密钥）
        if names.isEmpty {
            for (id, _) in providers {
                if let n = envName(ofProvider: id), !names.contains(n) { names.append(n) }
            }
        }
        return names.sorted()
    }

    /// 感知层的 system prompt（来自 prompts.json，与 Python/桌面版同一份）
    static var perceptionPrompt: String {
        (loadJSON("prompts")?["perception"] as? [String: Any])?["system_prompt"] as? String ?? ""
    }
    static var perceptionUserText: String {
        (loadJSON("prompts")?["perception"] as? [String: Any])?["user_text"] as? String
            ?? "请把这张聊天截图还原成结构化 JSON。"
    }

    /// 题目档案（one_on_one / group）与起草 prompt
    static var questionsFile: [String: Any]? { loadJSON("questions") }
}

/// 密钥只存 Keychain。**不进 UserDefaults、不进代码、不进日志。**
enum KeychainStore {

    private static let service = "com.jev.assistant.keys"

    static func save(_ value: String, for account: String) {
        guard !account.isEmpty else { return }
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }   // 传空串 = 删除
        var add = query
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    static func read(_ account: String) -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data,
              let s = String(data: data, encoding: .utf8) else { return "" }
        return s
    }

    static func has(_ account: String) -> Bool { !read(account).isEmpty }
}
