import SwiftUI

/// provider 管理：让用户自己选服务、自己填 URL 与 API 格式、自己维护模型列表。
///
/// 为什么要有这一页：providers.json 原本是包内只读的，换服务得改仓库重新构建——
/// 对已经装在手机上的人等于做不到。现在在这里改就行，改的是**覆盖层**
/// （`Documents/jev_providers_user.json`），包内那份仍是跨端单一来源。
///
/// 三件事刻意做对：
///   1. **密钥只进 Keychain**，这一页写的任何文件里都没有密钥。
///   2. 只写"与默认不同"的部分，所以以后仓库更新默认配置时，没改过的部分照样生效。
///   3. 「角色指定」与 provider 编辑分开：先决定"哪一步用哪家"，再进去调细节。
struct ProvidersView: View {
    @ObservedObject private var store = ProvidersStore.shared

    @State private var editing: ProvidersStore.ProviderEntry?
    @State private var showAdd = false
    @State private var newName = ""

    var body: some View {
        List {
            rolesSection
            providersSection
            aboutSection
        }
        .navigationTitle("Provider 与模型")
        .sheet(item: $editing) { p in
            // sheet 里的内容是**独立**的一棵视图树，不会继承外层的导航栈——
            // 不包 NavigationStack 的话，编辑页的标题栏与「保存」按钮都不会出现。
            NavigationStack {
                ProviderEditView(entry: p, store: store)
            }
        }
        .alert("新增 provider", isPresented: $showAdd) {
            TextField("名字，例如 我的中转", text: $newName)
            Button("取消", role: .cancel) { newName = "" }
            Button("添加") {
                if let e = store.addProvider(id: newName) { editing = e }
                newName = ""
            }
        } message: {
            Text("添加后填写 Base URL、API 格式、密钥，再添加至少一个模型。")
        }
    }

    // MARK: 角色指定

    private var rolesSection: some View {
        Section {
            ForEach(ProvidersStore.roles, id: \.self) { role in
                VStack(alignment: .leading, spacing: 6) {
                    Text(ProvidersStore.roleLabel(role)).font(.caption).foregroundStyle(.secondary)

                    Picker("provider", selection: Binding(
                        get: { store.roleProviders[role] ?? "" },
                        set: { store.setRoleProvider($0, for: role) })) {
                        ForEach(store.providers) { p in
                            Text(p.displayName).tag(p.id)
                        }
                    }
                    .pickerStyle(.menu)

                    Picker("模型", selection: Binding(
                        get: { store.roleModels[role] ?? "" },
                        set: { store.setRoleModel($0, for: role) })) {
                        ForEach(store.provider(for: role)?.models ?? []) { m in
                            Text(m.enabled ? m.displayName : "\(m.displayName)（已停用）")
                                .tag(m.id)
                        }
                    }
                    .pickerStyle(.menu)
                    .disabled((store.provider(for: role)?.models.isEmpty ?? true))

                    if let p = store.provider(for: role) {
                        Text(store.url(for: p))
                            .font(.caption2).foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                .padding(.vertical, 2)
            }
        } header: {
            Text("哪一步用哪家")
        } footer: {
            Text("判断＝七道题与排序；起草＝生成回复候选；感知＝读屏幕上的对话。"
                 + "三者互相独立，可以混着配（例如判断用 Jev，起草与感知用同一家的不同模型）。")
        }
    }

    // MARK: provider 列表

    private var providersSection: some View {
        Section {
            ForEach(store.providers) { p in
                Button {
                    editing = p
                } label: {
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 5) {
                                Text(p.displayName).font(.subheadline).bold()
                                if !KeychainStore.has(store.secretAccount(for: p)) {
                                    Text("未填密钥").font(.caption2).foregroundStyle(.orange)
                                }
                                if store.isModified(p) {
                                    Text("已改").font(.caption2).foregroundStyle(.blue)
                                }
                            }
                            Text(p.baseURL.isEmpty ? "未填 Base URL" : p.baseURL)
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            Text("\(p.enabledModels.count)/\(p.models.count) 个模型启用")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 4)
                        Image(systemName: "chevron.right")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .foregroundStyle(.primary)
                }
            }
            .onDelete { idx in
                // 先取出 id 再删：直接在循环里删会让后面的下标失效
                // （一次滑动删不涉及多个，但 `.onDelete` 给的是 IndexSet，不能假设只有一个）
                let ids = idx.compactMap { i in
                    store.providers.indices.contains(i) ? store.providers[i].id : nil
                }
                for id in ids { store.remove(id) }
            }

            Button {
                showAdd = true
            } label: {
                Label("添加 provider", systemImage: "plus")
            }

            if store.hiddenBuiltinCount > 0 {
                Button("恢复自带的 provider（已藏起 \(store.hiddenBuiltinCount) 个）") {
                    store.restoreBuiltins()
                }
                .font(.footnote)
            }
        } header: {
            Text("服务（左滑删除；自带的只是藏起来，可恢复）")
        } footer: {
            Text("改这里不需要重新构建 App。改动保存在 App 自己目录下的 "
                 + "jev_providers_user.json（**只有端点与模型，没有密钥**），"
                 + "删掉该文件就恢复出厂默认。")
        }
    }

    private var aboutSection: some View {
        Section {
            Text("密钥存在系统 Keychain 里，不写进上面那个文件，也不会出现在导出记录中。"
                 + "导出记录只含对话与分析文本。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

// MARK: - 单个 provider 的编辑页

/// 按参考界面组织：Base URL → API 格式 → API Key → 模型列表。
struct ProviderEditView: View {
    @State private var draft: ProvidersStore.ProviderEntry
    @ObservedObject var store: ProvidersStore
    @Environment(\.dismiss) private var dismiss

    @State private var keyText = ""
    @State private var revealKey = false
    @State private var keySaved = false
    @State private var editingModel: ProvidersStore.ModelEntry?
    @State private var showAddModel = false
    @State private var newModelID = ""

    init(entry: ProvidersStore.ProviderEntry, store: ProvidersStore) {
        _draft = State(initialValue: entry)
        self.store = store
    }

    private var account: String { store.secretAccount(for: draft) }
    private var hasStoredKey: Bool { KeychainStore.has(account) }

    var body: some View {
        Form {
            connectionSection
            keySection
            modelsSection
            roleSection
            dangerSection
        }
        .navigationTitle(draft.displayName)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("取消") { dismiss() }
            }
            ToolbarItem(placement: .confirmationAction) {
                Button("保存") { commitAndClose() }
            }
        }
        .sheet(item: $editingModel) { m in
            NavigationStack {
                ModelEditView(model: m) { updated in
                    if let i = draft.models.firstIndex(where: { $0.id == m.id }) {
                        draft.models[i] = updated
                    } else {
                        draft.models.append(updated)
                    }
                }
            }
        }
        .alert("添加模型", isPresented: $showAddModel) {
            TextField("模型 id，例如 deepseek-chat", text: $newModelID)
            Button("取消", role: .cancel) { newModelID = "" }
            Button("添加") {
                let id = newModelID.trimmingCharacters(in: .whitespaces)
                if !id.isEmpty, !draft.models.contains(where: { $0.id == id }) {
                    draft.models.append(ProvidersStore.ModelEntry(id: id, enabled: true))
                }
                newModelID = ""
            }
        } message: {
            Text("id 必须与服务方文档里一致。添加后可再点铅笔补上「上下文长度」和「视觉」等标注。")
        }
    }

    // MARK: Base URL / API 格式

    private var connectionSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 5) {
                Text("Base URL").font(.caption).foregroundStyle(.secondary)
                TextField("https://api.deepseek.com", text: $draft.baseURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .font(.footnote)
            }

            Picker("API 格式", selection: $draft.apiFormat) {
                ForEach(store.formats) { f in
                    Text("\(f.label)（\(f.path)）").tag(f.id)
                }
            }
            .onChange(of: draft.apiFormat) { _, newValue in
                // 换格式时把路径也跟着换：否则会留下上一个格式的 path，
                // 拼出一个谁也看不出来的错 URL。
                if let f = store.formats.first(where: { $0.id == newValue }) {
                    draft.path = f.path
                    draft.kind = f.kind
                }
            }

            if let f = store.formats.first(where: { $0.id == draft.apiFormat }), !f.hint.isEmpty {
                Text(f.hint).font(.caption2).foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 5) {
                Text("路径（一般不用改）").font(.caption).foregroundStyle(.secondary)
                TextField("/v1/chat/completions", text: $draft.path)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.footnote)
            }

            LabeledContent("实际请求地址") {
                Text(store.url(for: draft))
                    .font(.caption2).foregroundStyle(.secondary)
                    .multilineTextAlignment(.trailing)
            }
            LabeledContent("超时（秒）") {
                TextField("180", value: $draft.timeout, format: .number)
                    .keyboardType(.numberPad)
                    .multilineTextAlignment(.trailing)
            }
        } header: {
            Text("连接")
        } footer: {
            Text("Base URL 直接粘**完整地址**也没问题（例如 https://api.deepseek.com/v1/chat/completions）——"
                 + "发现已经带了路径就不会再拼一次。")
        }
    }

    // MARK: API Key

    private var keySection: some View {
        Section {
            HStack(spacing: 8) {
                if revealKey {
                    TextField(hasStoredKey ? "已保存（重新输入可覆盖）" : "粘贴 API Key", text: $keyText)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.footnote)
                } else {
                    SecureField(hasStoredKey ? "已保存（重新输入可覆盖）" : "粘贴 API Key", text: $keyText)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.footnote)
                }
                Button {
                    revealKey.toggle()
                } label: {
                    Image(systemName: revealKey ? "eye.slash" : "eye")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
            }

            Button(keySaved ? "已保存到 Keychain" : "保存密钥") {
                KeychainStore.save(keyText.trimmingCharacters(in: .whitespacesAndNewlines), for: account)
                keyText = ""
                keySaved = true
            }
            .disabled(keyText.isEmpty && !hasStoredKey)

            if hasStoredKey {
                Button("清除已保存的密钥", role: .destructive) {
                    KeychainStore.save("", for: account)
                    keySaved = false
                }
                .font(.footnote)
            }

            if draft.apiKeyOptional {
                Text("这家服务被标为密钥可选（一般用于本机服务）。")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        } header: {
            Text("API Key")
        } footer: {
            Text("存进系统 Keychain，账号名 \(account)。**不会**写进任何配置文件、"
                 + "不会进日志、不会随导出记录离开手机。")
        }
    }

    // MARK: 模型列表

    private var modelsSection: some View {
        Section {
            ForEach(draft.models) { m in
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 5) {
                            Text(m.displayName).font(.footnote)
                            if !m.context.isEmpty {
                                TagChip(text: m.context)
                            }
                            ForEach(m.tags, id: \.self) { t in
                                TagChip(text: t, highlight: t == "视觉")
                            }
                        }
                        if m.displayName != m.id {
                            Text(m.id).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer(minLength: 4)
                    Button {
                        editingModel = m
                    } label: {
                        Image(systemName: "pencil").font(.caption)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    Button {
                        draft.models.removeAll { $0.id == m.id }
                    } label: {
                        Image(systemName: "trash").font(.caption)
                    }
                    .buttonStyle(.plain).foregroundStyle(.red)
                    Toggle("", isOn: Binding(
                        get: { m.enabled },
                        set: { v in
                            if let i = draft.models.firstIndex(where: { $0.id == m.id }) {
                                draft.models[i].enabled = v
                            }
                        }))
                        .labelsHidden()
                }
            }

            Button {
                showAddModel = true
            } label: {
                Label("添加模型", systemImage: "plus")
            }
        } header: {
            Text("模型列表")
        } footer: {
            Text("停用的模型不会出现在「哪一步用哪家」的下拉里，也就不会被选中——"
                 + "排查某个模型时比删掉再敲回来省事。")
        }
    }

    // MARK: 这个 provider 被哪些角色用到

    private var roleSection: some View {
        Section("被这些角色使用") {
            let used = ProvidersStore.roles.filter { store.roleProviders[$0] == draft.id }
            if used.isEmpty {
                Text("目前没有角色用它（在上一页的「哪一步用哪家」里指过来即可）。")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(used, id: \.self) { r in
                    LabeledContent(ProvidersStore.roleLabel(r),
                                   value: store.roleModels[r] ?? "—")
                }
            }
        }
    }

    private var dangerSection: some View {
        Section {
            Button(draft.isBuiltin ? "隐藏这家 provider" : "删除这家 provider", role: .destructive) {
                store.remove(draft.id)
                dismiss()
            }
            if !draft.isBuiltin {
                LabeledContent("标识", value: draft.id)
            }
        }
    }

    private func commitAndClose() {
        if !keyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            KeychainStore.save(keyText.trimmingCharacters(in: .whitespacesAndNewlines), for: account)
        }
        // 没填 path 就按格式补齐，避免存下一个空路径
        if draft.path.isEmpty { draft.path = store.effectivePath(draft) }
        draft.label = draft.label.trimmingCharacters(in: .whitespaces)
        store.update(draft)
        dismiss()
    }
}

// MARK: - 单个模型的编辑

struct ModelEditView: View {
    @State private var draft: ProvidersStore.ModelEntry
    let onSave: (ProvidersStore.ModelEntry) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var newTag = ""

    init(model: ProvidersStore.ModelEntry, onSave: @escaping (ProvidersStore.ModelEntry) -> Void) {
        _draft = State(initialValue: model)
        self.onSave = onSave
    }

    private static let suggestedTags = ["视觉", "判断", "快", "贵"]

    var body: some View {
        Form {
            Section("模型") {
                LabeledContent("id", value: draft.id)
                TextField("显示名（可留空）", text: $draft.label)
                    .textInputAutocapitalization(.never)
            }
            Section {
                TextField("例如 1M / 128K", text: $draft.context)
                    .textInputAutocapitalization(.never)
            } header: {
                Text("上下文长度（仅用于显示）")
            } footer: {
                Text("只是给你自己看的标注。Jev 不按它做截断。")
            }
            Section("能力标注") {
                ForEach(draft.tags, id: \.self) { t in
                    HStack {
                        Text(t)
                        Spacer()
                        Button {
                            draft.tags.removeAll { $0 == t }
                        } label: { Image(systemName: "minus.circle").foregroundStyle(.red) }
                            .buttonStyle(.plain)
                    }
                }
                HStack {
                    TextField("加一个标注", text: $newTag)
                    Button("加") {
                        let t = newTag.trimmingCharacters(in: .whitespaces)
                        if !t.isEmpty, !draft.tags.contains(t) { draft.tags.append(t) }
                        newTag = ""
                    }
                }
                HStack(spacing: 6) {
                    ForEach(Self.suggestedTags, id: \.self) { t in
                        Button(t) {
                            if !draft.tags.contains(t) { draft.tags.append(t) }
                        }
                        .font(.caption)
                        .buttonStyle(.bordered)
                    }
                }
            }
            Section {
                Toggle("启用", isOn: $draft.enabled)
            } footer: {
                Text("「视觉」这个标注很重要：感知层要读图，选了没有视觉能力的模型会一直失败。")
            }
        }
        .navigationTitle("编辑模型")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button("完成") { onSave(draft); dismiss() }
            }
        }
    }
}

/// 小标签（1M / 视觉）
struct TagChip: View {
    let text: String
    var highlight: Bool = false

    var body: some View {
        Text(text)
            .font(.system(size: 10))
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(highlight ? Color.accentColor.opacity(0.18) : Color.gray.opacity(0.16)))
            .foregroundStyle(highlight ? Color.accentColor : Color.secondary)
    }
}
