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

    /// 同 `loadJSON`，名字更明确：**只读包内**那份、不含用户覆盖层。
    /// ProvidersStore 要用它拿"出厂默认值"来做 diff。
    static func loadBundleJSON(_ name: String) -> [String: Any]? { loadJSON(name) }

    /// providers.json 里的一个 provider（已合并用户覆盖层，模型已按角色解析）
    struct Provider {
        let id: String
        let label: String
        let kind: String            // systemone | openai_chat
        let apiFormat: String       // chat_completions | systemone
        let baseURL: String
        let path: String
        let model: String
        let apiKeyEnv: String
        /// Keychain 里的账号名。自带 provider 等于 `api_key_env`（所以老用户已存的
        /// 密钥继续有效）；用户自加的 provider 没有环境变量名，用生成的 `USER_<ID>`。
        let keyAccount: String
        let apiKeyOptional: Bool
        let maxTokens: Int?
        let timeout: Double
        let extraHeaders: [String: String]
        let url: String
    }

    /// 按角色取 provider。
    ///
    /// 端点、模型、启用位都来自 `ProvidersStore`（包内默认 + 用户在设置页改的覆盖层），
    /// 所以用户在 App 里换个中转地址或换个模型，**不需要重新构建**。
    static func provider(role: String, override: String? = nil) -> Provider? {
        let store = ProvidersStore.shared
        let entry: ProvidersStore.ProviderEntry?
        if let o = override {
            entry = store.providers.first { $0.id == o }
        } else {
            entry = store.provider(for: role)
        }
        guard let e = entry else { return nil }
        // 模型按**角色**解析（role_models），不是取 provider 的第一个：
        // 「同一个服务，判断用 A 模型、起草用 B 模型」靠的就是这一步。
        let model = override == nil ? store.model(for: role) : (e.enabledModels.first?.id ?? "")
        return Provider(
            id: e.id,
            label: e.displayName,
            kind: e.kind,
            apiFormat: e.apiFormat,
            baseURL: e.baseURL,
            path: store.effectivePath(e),
            model: model,
            apiKeyEnv: e.apiKeyEnv,
            keyAccount: store.secretAccount(for: e),
            apiKeyOptional: e.apiKeyOptional,
            maxTokens: store.maxTokens(for: role) ?? e.maxTokens,
            timeout: e.timeout,
            extraHeaders: e.extraHeaders,
            url: store.url(for: e)
        )
    }

    /// 设置页要显示的密钥字段 = **所有 provider 的密钥账号**。
    ///
    /// 以前只列三个角色实际用到的（免得给用户看用不上的输入框）。现在用户能自己加
    /// provider、也能把角色指到任意一家，"用不到的"已经不存在了——全列才对。
    /// 顺序上把角色正在用的排前面。
    static func allKeyNames() -> [String] {
        let store = ProvidersStore.shared
        let active = ProvidersStore.roles.compactMap { store.provider(for: $0)?.keyAccount }
        let all = store.providers.map(\.keyAccount).filter { !$0.isEmpty }
        var out: [String] = []
        for n in active + all where !out.contains(n) { out.append(n) }
        return out
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
