import Foundation

/// providers 的可编辑配置：**包内默认值 + 用户覆盖层**。
///
/// 为什么要这一层：原来 providers.json 是包内只读的，用户想换一个中转地址、
/// 换个模型、或者接入一个我们没内置的服务，必须改仓库再重新构建——这对
/// 已经装到手机上的人等于做不到。现在设置页可以直接改。
///
/// 三条设计约束：
///  1. **密钥绝不写进任何文件**。覆盖层里只有端点、模型、启用位；密钥进 Keychain
///     （见 `KeychainStore`）。所以本文件写出的 JSON 可以随便看、随便备份。
///  2. **覆盖层只写"与默认不同的部分"**，与 PC 侧 `providers.py` 的 overlay 语义一致。
///     这样以后仓库更新了默认配置（比如新加一家 provider），用户没改过的部分照样生效。
///  3. **包内那份仍是跨端单一来源**。这里只做合并，不重新定义默认值——
///     默认值改在 `tools/jev/providers.json`，三端一起变。
final class ProvidersStore: ObservableObject {

    static let shared = ProvidersStore()

    // MARK: 数据模型

    struct ModelEntry: Codable, Identifiable, Hashable {
        var id: String
        var label: String = ""
        var context: String = ""       // 上下文长度标（"1M" / "128K"），只用于显示
        var tags: [String] = []        // 能力标（"视觉" / "判断"），只用于显示与筛人眼
        var enabled: Bool = true       // 关掉的模型不会被角色选中
        var maxTokens: Int? = nil

        var displayName: String { label.isEmpty ? id : label }
        var isVision: Bool { tags.contains("视觉") }
    }

    struct ProviderEntry: Identifiable, Hashable {
        var id: String
        var label: String = ""
        var baseURL: String = ""
        var apiFormat: String = "chat_completions"
        var path: String = ""
        var kind: String = "openai_chat"
        var apiKeyEnv: String = ""
        var apiKeyOptional: Bool = false
        var timeout: Double = 60
        var maxTokens: Int? = nil
        var extraHeaders: [String: String] = [:]
        var models: [ModelEntry] = []
        /// 包内自带的（不能真删，只能隐藏）；用户自加的没有这个标记
        var isBuiltin: Bool = false

        var displayName: String { label.isEmpty ? id : label }
        var enabledModels: [ModelEntry] { models.filter { $0.enabled } }

        /// Keychain 里的账号名。
        ///
        /// 自带 provider 用它声明的 `api_key_env`（**老用户已存的密钥因此继续有效**）；
        /// 用户自加的 provider 没有环境变量名，就用 `USER_<ID>` 生成一个。
        var keyAccount: String {
            if !apiKeyEnv.trimmingCharacters(in: .whitespaces).isEmpty { return apiKeyEnv }
            return "USER_" + id.uppercased()
        }
    }

    struct FormatEntry: Identifiable, Hashable {
        var id: String
        var label: String
        var path: String
        var kind: String
        var hint: String
    }

    // MARK: 状态

    @Published private(set) var providers: [ProviderEntry] = []
    @Published private(set) var formats: [FormatEntry] = []
    @Published private(set) var roleProviders: [String: String] = [:]
    @Published private(set) var roleModels: [String: String] = [:]

    /// 用户从自带列表里"删掉"的 provider id（隐藏，而不是从默认里移除）
    private var hiddenBuiltinIDs: Set<String> = []
    /// 包内默认值的快照：写覆盖层时用它算 diff
    private var builtinSnapshot: [String: ProviderEntry] = [:]

    static let roles = ["judge", "analysis", "perception"]
    static func roleLabel(_ r: String) -> String {
        switch r {
        case "judge": return "判断（Jev 七道题）"
        case "analysis": return "起草（生成回复候选）"
        case "perception": return "感知（读截图）"
        default: return r
        }
    }

    private let userURL: URL = {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("jev_providers_user.json")
    }()

    // MARK: 载入

    private init() {
        loadBuiltin()
        loadOverlay()
        normalizeRoles()
    }

    private func loadBuiltin() {
        guard let cfg = BrainConfig.loadBundleJSON("providers") else {
            providers = []
            return
        }
        formats = parseFormats(cfg)
        let map = (cfg["providers"] as? [String: Any]) ?? [:]
        var out: [ProviderEntry] = []
        for (pid, raw) in map {
            guard let d = raw as? [String: Any] else { continue }
            var e = entry(from: d, id: pid)
            e.isBuiltin = true
            out.append(e)
        }
        // 顺序：先按 roles 里出现的顺序，其余按 label 排——让常用的排在前面
        providers = out.sorted { $0.displayName < $1.displayName }
        builtinSnapshot = Dictionary(uniqueKeysWithValues: out.map { ($0.id, $0) })

        let roles = (cfg["roles"] as? [String: Any]) ?? [:]
        // 过滤掉 `_comment` 这类以下划线开头的说明项，否则会多出一个名为 "_comment" 的"角色"
        roleProviders = roles
            .filter { !$0.key.hasPrefix("_") }
            .compactMapValues { $0 as? String }
        roleModels = ((cfg["role_models"] as? [String: Any]) ?? [:])
            .filter { !$0.key.hasPrefix("_") }
            .compactMapValues { $0 as? String }
    }

    private func parseFormats(_ cfg: [String: Any]) -> [FormatEntry] {
        let raw = (cfg["api_formats"] as? [String: Any]) ?? [:]
        var out: [FormatEntry] = []
        for (fid, v) in raw {
            if fid.hasPrefix("_") { continue }
            guard let d = v as? [String: Any] else { continue }
            out.append(FormatEntry(id: fid,
                                   label: (d["label"] as? String) ?? fid,
                                   path: (d["path"] as? String) ?? "",
                                   kind: (d["kind"] as? String) ?? "openai_chat",
                                   hint: (d["hint"] as? String) ?? ""))
        }
        return out.sorted { $0.label < $1.label }
    }

    private func entry(from d: [String: Any], id: String) -> ProviderEntry {
        var e = ProviderEntry(id: id)
        e.label = (d["label"] as? String) ?? ""
        e.baseURL = (d["base_url"] as? String) ?? ""
        e.apiFormat = (d["api_format"] as? String) ?? formatInferring(from: d)
        e.path = (d["path"] as? String) ?? ""
        e.kind = (d["kind"] as? String) ?? kindFor(e.apiFormat)
        e.apiKeyEnv = (d["api_key_env"] as? String) ?? ""
        e.apiKeyOptional = (d["api_key_optional"] as? Bool) ?? false
        e.timeout = (d["timeout_default"] as? Double) ?? 60
        e.maxTokens = d["max_tokens"] as? Int
        e.extraHeaders = (d["extra_headers"] as? [String: String]) ?? [:]
        e.models = ((d["models"] as? [[String: Any]]) ?? []).compactMap { m in
            guard let mid = m["id"] as? String else { return nil }
            return ModelEntry(id: mid,
                              label: (m["label"] as? String) ?? "",
                              context: (m["context"] as? String) ?? "",
                              tags: (m["tags"] as? [String]) ?? [],
                              enabled: (m["enabled"] as? Bool) ?? true,
                              maxTokens: m["max_tokens"] as? Int)
        }
        // v1 兼容：老配置只有单个 model 字段，没有 models[]。补成一条模型。
        if e.models.isEmpty, let single = d["model"] as? String, !single.isEmpty {
            e.models = [ModelEntry(id: single, enabled: true)]
        }
        return e
    }

    private func formatInferring(from d: [String: Any]) -> String {
        (d["kind"] as? String) == "systemone" ? "systemone" : "chat_completions"
    }

    private func kindFor(_ format: String) -> String {
        formats.first { $0.id == format }?.kind ?? "openai_chat"
    }

    private func loadOverlay() {
        guard let data = try? Data(contentsOf: userURL),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }

        if let roles = obj["roles"] as? [String: String] { roleProviders.merge(roles) { _, new in new } }
        if let rm = obj["role_models"] as? [String: String] { roleModels.merge(rm) { _, new in new } }
        if let del = obj["deleted"] as? [String] { hiddenBuiltinIDs = Set(del) }

        if let patch = obj["providers"] as? [String: Any] {
            for (pid, raw) in patch {
                guard let d = raw as? [String: Any] else { continue }
                if let i = providers.firstIndex(where: { $0.id == pid }) {
                    apply(d, to: &providers[i])
                } else {
                    // 用户自加的：整条来自覆盖层
                    providers.append(entry(from: d, id: pid))
                }
            }
        }
        providers = providers.filter { !hiddenBuiltinIDs.contains($0.id) }
            .sorted { $0.displayName < $1.displayName }
    }

    /// 只覆盖覆盖层里写了的字段（与 PC 侧 providers.py 的合并语义一致）
    private func apply(_ d: [String: Any], to e: inout ProviderEntry) {
        if let v = d["label"] as? String { e.label = v }
        if let v = d["base_url"] as? String { e.baseURL = v }
        if let v = d["api_format"] as? String { e.apiFormat = v }
        if let v = d["path"] as? String { e.path = v }
        if let v = d["kind"] as? String { e.kind = v }
        if let v = d["api_key_env"] as? String { e.apiKeyEnv = v }
        if let v = d["api_key_optional"] as? Bool { e.apiKeyOptional = v }
        if let v = d["timeout_default"] as? Double { e.timeout = v }
        if let v = d["max_tokens"] as? Int { e.maxTokens = v }
        if let v = d["extra_headers"] as? [String: String] { e.extraHeaders = v }
        if let ms = d["models"] as? [[String: Any]] {
            e.models = ms.compactMap { m in
                guard let mid = m["id"] as? String else { return nil }
                return ModelEntry(id: mid,
                                  label: (m["label"] as? String) ?? "",
                                  context: (m["context"] as? String) ?? "",
                                  tags: (m["tags"] as? [String]) ?? [],
                                  enabled: (m["enabled"] as? Bool) ?? true,
                                  maxTokens: m["max_tokens"] as? Int)
            }
        }
    }

    /// 角色指向的 provider 若不存在或没启用模型，退回到第一个可用项——
    /// 否则用户删掉一个 provider 之后，三个角色会一起失效且没有提示。
    private func normalizeRoles() {
        for role in Self.roles {
            let ok = roleProviders[role].flatMap { id in providers.first { $0.id == id } }
            if ok == nil || ok?.enabledModels.isEmpty == true {
                if let first = providers.first(where: { !$0.enabledModels.isEmpty }) {
                    roleProviders[role] = first.id
                }
            }
            guard let pid = roleProviders[role],
                  let p = providers.first(where: { $0.id == pid }) else { continue }
            let want = roleModels[role] ?? ""
            if !p.enabledModels.contains(where: { $0.id == want }) {
                roleModels[role] = p.enabledModels.first?.id ?? ""
            }
        }
    }

    // MARK: 查询（运行时用）

    /// 角色解析到的 provider
    func provider(for role: String) -> ProviderEntry? {
        guard let pid = roleProviders[role] else { return nil }
        return providers.first { $0.id == pid }
    }

    /// 角色解析到的模型：优先 role_models，其次该 provider 的第一个启用模型
    func model(for role: String) -> String {
        guard let p = provider(for: role) else { return "" }
        if let want = roleModels[role], p.enabledModels.contains(where: { $0.id == want }) { return want }
        return p.enabledModels.first?.id ?? ""
    }

    /// 该模型对应的 max_tokens（模型级优先，其次 provider 级）
    func maxTokens(for role: String) -> Int? {
        guard let p = provider(for: role) else { return nil }
        let m = model(for: role)
        if let hit = p.models.first(where: { $0.id == m }), let mt = hit.maxTokens { return mt }
        return p.maxTokens
    }

    /// 完整端点。**与 PC 侧 build_url 同一条规则**：
    /// 用户很可能把完整 URL 粘进 Base URL，再拼一次就变成
    /// .../chat/completions/chat/completions（404 且很难查）。
    func url(for p: ProviderEntry) -> String {
        let base = p.baseURL.hasSuffix("/") ? String(p.baseURL.dropLast()) : p.baseURL
        let path = effectivePath(p)
        if path.isEmpty { return base }
        if base.hasSuffix(path) || base.hasSuffix(String(path.drop(while: { $0 == "/" }))) { return base }
        return base + path
    }

    /// 用户没写 path 就按 API 格式推
    func effectivePath(_ p: ProviderEntry) -> String {
        if !p.path.isEmpty { return p.path }
        return formats.first { $0.id == p.apiFormat }?.path ?? ""
    }

    func secretAccount(for p: ProviderEntry) -> String { p.keyAccount }

    // MARK: 修改（设置页用）

    func update(_ p: ProviderEntry) {
        if let i = providers.firstIndex(where: { $0.id == p.id }) { providers[i] = p }
        else { providers.append(p) }
        providers.sort { $0.displayName < $1.displayName }
        normalizeRoles()
        save()
    }

    func addProvider(id: String) -> ProviderEntry? {
        let slug = Self.slug(id)
        guard !slug.isEmpty, !providers.contains(where: { $0.id == slug }) else { return nil }
        var e = ProviderEntry(id: slug)
        e.label = id
        e.apiFormat = formats.first?.id ?? "chat_completions"
        e.kind = kindFor(e.apiFormat)
        e.path = formats.first?.path ?? ""
        e.timeout = 180
        e.models = []
        providers.append(e)
        providers.sort { $0.displayName < $1.displayName }
        save()
        return e
    }

    func remove(_ id: String) {
        guard let p = providers.first(where: { $0.id == id }) else { return }
        providers.removeAll { $0.id == id }
        if p.isBuiltin { hiddenBuiltinIDs.insert(id) }   // 自带的只隐藏，便于"恢复"
        normalizeRoles()
        save()
    }

    /// 把自带 provider 恢复回来（用户删过之后想找回来）
    func restoreBuiltins() {
        hiddenBuiltinIDs.removeAll()
        loadBuiltin()
        loadOverlay()
        normalizeRoles()
        save()
    }

    var hiddenBuiltinCount: Int { hiddenBuiltinIDs.count }

    func setRoleProvider(_ pid: String, for role: String) {
        roleProviders[role] = pid
        normalizeRoles()
        save()
    }

    func setRoleModel(_ model: String, for role: String) {
        roleModels[role] = model
        save()
    }

    static func slug(_ raw: String) -> String {
        let lowered = raw.lowercased()
        let allowed = Set("abcdefghijklmnopqrstuvwxyz0123456789_")
        let mapped = lowered.map { ch -> Character in
            if allowed.contains(ch) { return ch }
            if ch == " " || ch == "-" || ch == "." { return "_" }
            return "_"
        }
        return String(mapped).trimmingCharacters(in: CharacterSet(charactersIn: "_"))
    }

    // MARK: 保存（只写与默认不同的部分）

    private func save() {
        var provPatch: [String: Any] = [:]
        for p in providers {
            if let b = builtinSnapshot[p.id] {
                if let d = diff(p, b) { provPatch[p.id] = d }
            } else {
                provPatch[p.id] = fullDict(p)      // 用户自加的：整条写
            }
        }
        var out: [String: Any] = [
            "_comment": "App 设置页写出的**用户覆盖层**。只含与包内默认不同的部分。"
                + "**不含任何密钥**（密钥在 Keychain 里）。删掉本文件即可恢复默认。",
            "providers": provPatch,
            "roles": roleProviders,
            "role_models": roleModels,
        ]
        if !hiddenBuiltinIDs.isEmpty { out["deleted"] = Array(hiddenBuiltinIDs).sorted() }
        guard let data = try? JSONSerialization.data(withJSONObject: out,
                                                     options: [.prettyPrinted, .sortedKeys]) else { return }
        try? data.write(to: userURL, options: .atomic)
    }

    private func modelsArray(_ ms: [ModelEntry]) -> [[String: Any]] {
        ms.map { m in
            var d: [String: Any] = ["id": m.id, "enabled": m.enabled]
            if !m.label.isEmpty { d["label"] = m.label }
            if !m.context.isEmpty { d["context"] = m.context }
            if !m.tags.isEmpty { d["tags"] = m.tags }
            if let mt = m.maxTokens { d["max_tokens"] = mt }
            return d
        }
    }

    private func fullDict(_ p: ProviderEntry) -> [String: Any] {
        var d: [String: Any] = [
            "label": p.label, "base_url": p.baseURL, "api_format": p.apiFormat,
            "path": p.path, "kind": p.kind, "timeout_default": p.timeout,
            "models": modelsArray(p.models),
        ]
        if !p.apiKeyEnv.isEmpty { d["api_key_env"] = p.apiKeyEnv }
        if p.apiKeyOptional { d["api_key_optional"] = true }
        if let mt = p.maxTokens { d["max_tokens"] = mt }
        if !p.extraHeaders.isEmpty { d["extra_headers"] = p.extraHeaders }
        return d
    }

    private func diff(_ p: ProviderEntry, _ b: ProviderEntry) -> [String: Any]? {
        var d: [String: Any] = [:]
        if p.label != b.label { d["label"] = p.label }
        if p.baseURL != b.baseURL { d["base_url"] = p.baseURL }
        if p.apiFormat != b.apiFormat { d["api_format"] = p.apiFormat }
        if p.path != b.path { d["path"] = p.path }
        if p.kind != b.kind { d["kind"] = p.kind }
        if p.apiKeyEnv != b.apiKeyEnv { d["api_key_env"] = p.apiKeyEnv }
        if p.apiKeyOptional != b.apiKeyOptional { d["api_key_optional"] = p.apiKeyOptional }
        if p.timeout != b.timeout { d["timeout_default"] = p.timeout }
        if p.maxTokens != b.maxTokens { d["max_tokens"] = p.maxTokens as Any }
        if p.extraHeaders != b.extraHeaders { d["extra_headers"] = p.extraHeaders }
        if p.models != b.models { d["models"] = modelsArray(p.models) }
        return d.isEmpty ? nil : d
    }

    /// 给设置页显示"这个改了、那个没改"
    func isModified(_ p: ProviderEntry) -> Bool {
        guard let b = builtinSnapshot[p.id] else { return true }
        return diff(p, b) != nil
    }
}
