import SwiftUI

/// 设置页：入口是「Provider 与模型」，下面是当前指向的自检。
///
/// 密钥字段是**按 provider 的密钥账号自动生成**的，不在代码里写死——
/// 用户自己加一家 provider，这一页会自动多出一个输入框。
struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var store = ProvidersStore.shared

    @State private var values: [String: String] = [:]
    @State private var saved = false
    @State private var showAdvanced = false

    private var keyNames: [String] { BrainConfig.allKeyNames() }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    NavigationLink {
                        ProvidersView()
                    } label: {
                        Label("Provider 与模型", systemImage: "server.rack")
                    }
                } footer: {
                    Text("换服务、改 Base URL、选 API 格式、填密钥、维护模型列表都在这里。"
                         + "改完不需要重新构建 App。")
                }

                Section("当前指向") {
                    ForEach(ProvidersStore.roles, id: \.self) { role in
                        if let p = BrainConfig.provider(role: role) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(ProvidersStore.roleLabel(role))
                                    .font(.caption).foregroundStyle(.secondary)
                                Text("\(p.label) · \(p.model)").font(.footnote)
                                Text(p.url).font(.system(size: 10)).foregroundStyle(.secondary)
                                let ready = p.keyAccount.isEmpty || KeychainStore.has(p.keyAccount)
                                Label(ready ? "密钥已就绪" : "缺少密钥（\(p.keyAccount)）",
                                      systemImage: ready ? "checkmark.circle" : "exclamationmark.triangle")
                                    .font(.caption2)
                                    .foregroundStyle(ready ? .green : .orange)
                            }
                        } else {
                            Text("\(role)：没有可用的 provider")
                                .font(.footnote).foregroundStyle(.orange)
                        }
                    }
                }

                Section {
                    DisclosureGroup("按账号名直接填密钥（高级）", isExpanded: $showAdvanced) {
                        if keyNames.isEmpty {
                            Text("没有可填的密钥字段").foregroundStyle(.secondary)
                        }
                        ForEach(keyNames, id: \.self) { name in
                            VStack(alignment: .leading, spacing: 4) {
                                HStack(spacing: 5) {
                                    Text(name).font(.caption).foregroundStyle(.secondary)
                                    if KeychainStore.has(name) {
                                        Image(systemName: "checkmark.circle.fill")
                                            .font(.caption2).foregroundStyle(.green)
                                    }
                                }
                                SecureField("粘贴密钥", text: binding(for: name))
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                                    .font(.system(.footnote, design: .monospaced))
                            }
                        }
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
                } footer: {
                    Text("平时不用管这里——在「Provider 与模型」里逐家填更直观。"
                         + "密钥只存本机 Keychain，不上传、不进日志、不随导出记录离开手机。")
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
