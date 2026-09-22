import SwiftUI

/// 设置页：填各家 API 密钥（存 Keychain），以及看配置是否齐备。
///
/// 密钥字段是**按 providers.json 里声明的环境变量名自动生成**的，不在代码里写死
/// ——加一家 provider 只需改配置，这一页会自动多出一个输入框。
struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss

    @State private var values: [String: String] = [:]
    @State private var saved = false

    private let keyNames = BrainConfig.allKeyNames()

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    if keyNames.isEmpty {
                        Text("providers.json 里没有声明任何密钥环境变量").foregroundStyle(.secondary)
                    }
                    ForEach(keyNames, id: \.self) { name in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(name).font(.caption).foregroundStyle(.secondary)
                            SecureField("粘贴密钥", text: binding(for: name))
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .font(.system(.footnote, design: .monospaced))
                        }
                    }
                } header: {
                    Text("API 密钥")
                } footer: {
                    Text("密钥只存在本机 Keychain（App 私有存储），不上传、不进日志。"
                         + "字段名来自 providers.json 的 api_key_env，改配置就会增减。")
                }

                Section {
                    Button {
                        for name in keyNames { KeychainStore.save(values[name] ?? "", for: name) }
                        saved = true
                    } label: {
                        Label("保存到 Keychain", systemImage: "key.fill")
                    }
                    if saved {
                        Label("已保存", systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green).font(.footnote)
                    }
                }

                Section("当前 provider 指向") {
                    ForEach(["judge", "analysis", "perception"], id: \.self) { role in
                        if let p = BrainConfig.provider(role: role) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(role).font(.caption).foregroundStyle(.secondary)
                                Text("\(p.id) · \(p.model)").font(.footnote)
                                Text(p.url).font(.system(size: 10)).foregroundStyle(.secondary)
                                Label(p.apiKeyEnv.isEmpty || KeychainStore.has(p.apiKeyEnv)
                                      ? "密钥已就绪" : "缺少 \(p.apiKeyEnv)",
                                      systemImage: p.apiKeyEnv.isEmpty || KeychainStore.has(p.apiKeyEnv)
                                      ? "checkmark.circle" : "exclamationmark.triangle")
                                    .font(.caption2)
                                    .foregroundStyle(p.apiKeyEnv.isEmpty || KeychainStore.has(p.apiKeyEnv)
                                                     ? .green : .orange)
                            }
                        } else {
                            Text("\(role)：providers.json 未配置").font(.footnote).foregroundStyle(.orange)
                        }
                    }
                }
            }
            .navigationTitle("设置")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                }
            }
            .onAppear {
                for name in keyNames { values[name] = KeychainStore.read(name) }
            }
        }
    }

    private func binding(for name: String) -> Binding<String> {
        Binding(get: { values[name] ?? "" }, set: { values[name] = $0; saved = false })
    }
}
