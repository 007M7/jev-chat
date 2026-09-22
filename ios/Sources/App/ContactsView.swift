import SwiftUI

/// 联系人目录：可查看、可检索、可编辑的人物身份。
///
/// 为什么单独成一个页面：人物身份是**跨会话通用**的（同一个人在多个群里出现），
/// 之前只能在"某个会话的详情里"顺带填，人不多了还凑合，一旦积累起来就没法查。
/// 用户明确提出要"可查看检索的联系人目录"。
///
/// 与记忆档案的关系：这里是**人工权威值**；将来从对话蒸馏出来的记忆是候选值，
/// 冲突时以人工为准（见 docs/design/functional_spec.md）。
struct ContactsView: View {
    @ObservedObject private var store = AnalysisStore.shared
    @State private var query = ""
    @State private var editing: Person?

    private var filtered: [Person] {
        let all = store.knownPeople
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return all }
        return all.filter { p in
            p.displayName.lowercased().contains(q)
                || p.identity.lowercased().contains(q)
                || p.aliases.contains { $0.lowercased().contains(q) }
        }
    }

    /// 按首字分组，像通讯录一样
    private var grouped: [(String, [Person])] {
        let dict = Dictionary(grouping: filtered) { p -> String in
            let first = p.displayName.trimmingCharacters(in: .whitespaces).first.map(String.init) ?? "#"
            return first.range(of: "[A-Za-z]", options: .regularExpression) != nil ? "#" : first
        }
        return dict.map { ($0.key, $0.value.sorted { $0.displayName < $1.displayName }) }
            .sorted { $0.0 < $1.0 }
    }

    var body: some View {
        NavigationStack {
            List {
                if store.knownPeople.isEmpty {
                    Section {
                        Text("还没有联系人。App 在分析时会自动登记发言人，之后可以在这里给每个人写身份描述。")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    ForEach(grouped, id: \.0) { title, people in
                        Section(title) {
                            ForEach(people) { p in
                                Button {
                                    editing = p
                                } label: {
                                    HStack(spacing: 11) {
                                        AvatarView(title: p.displayName, isGroup: false)
                                        VStack(alignment: .leading, spacing: 3) {
                                            HStack(spacing: 5) {
                                                Text(p.displayName).font(.subheadline).bold()
                                                if !p.identity.isEmpty {
                                                    Image(systemName: "checkmark.seal.fill")
                                                        .font(.caption2).foregroundStyle(.green)
                                                }
                                            }
                                            Text(p.identity.isEmpty ? "未填身份" : p.identity)
                                                .font(.caption)
                                                .foregroundStyle(p.identity.isEmpty ? .secondary : .primary)
                                                .lineLimit(1)
                                            if !p.aliases.isEmpty {
                                                Text("另读到过：" + p.aliases.joined(separator: "、"))
                                                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                            }
                                        }
                                        Spacer(minLength: 4)
                                        Text("\(p.seenCount) 次").font(.caption2).foregroundStyle(.secondary)
                                    }
                                    // List 里的 Button 会把文字染成强调色，这里显式还原成普通行样式
                                    .foregroundStyle(.primary)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("联系人")
            .searchable(text: $query, prompt: "搜名字、身份或别名")
            .sheet(item: $editing) { p in
                PersonEditView(person: p, store: store)
            }
        }
    }
}

/// 单个人物的编辑页
struct PersonEditView: View {
    let person: Person
    @ObservedObject var store: AnalysisStore
    @Environment(\.dismiss) private var dismiss
    @State private var draft: String = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("他是谁 / 什么角色", text: $draft, axis: .vertical)
                        .font(.footnote)
                } header: {
                    Text(person.displayName)
                } footer: {
                    Text("这条身份**跨会话通用**——填一次，他在任何群里说话时判断层都知道他是谁。")
                }
                if !person.aliases.isEmpty {
                    Section("视觉模型读到过的其他写法") {
                        Text(person.aliases.joined(separator: "、")).font(.footnote)
                        Text("同一个昵称常被读成几种写法（实测有名字被读成三种），"
                             + "别名用于归并到同一个人。")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                Section("出现情况") {
                    LabeledContent("被记为发言人", value: "\(person.seenCount) 次")
                }
            }
            .navigationTitle("编辑联系人")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        store.setPersonIdentity(draft, name: person.displayName)
                        dismiss()
                    }
                }
            }
            .onAppear { draft = person.identity }
        }
    }
}
