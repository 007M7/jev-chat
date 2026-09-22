import SwiftUI

/// 记录页：按会话（群聊 / 单聊）分组的分析历史 + 会话档案编辑。
///
/// 两个用途：
///   1. **可查的记录**——类似聊天记录，能回看每次分析的结论与候选
///   2. **人物 memory 的入口**——人工把"这个群是什么、这个人是谁"写清楚，
///      注入判断层。这是提升准确度最直接的一环：题目集原本要猜关系前提。
struct HistoryView: View {
    @ObservedObject private var store = AnalysisStore.shared

    var body: some View {
        NavigationStack {
            List {
                if store.sessions.isEmpty {
                    Section {
                        Text("还没有记录。开始采集后，每次自动分析都会记在这里。")
                            .foregroundStyle(.secondary)
                    }
                }
                ForEach(store.sessions, id: \.key) { s in
                    NavigationLink {
                        SessionDetailView(sessionKey: s.key, store: store)
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 6) {
                                Image(systemName: s.isGroup ? "person.3.fill" : "person.fill")
                                    .font(.caption).foregroundStyle(.secondary)
                                Text(s.title).font(.subheadline).bold().lineLimit(1)
                            }
                            Text("\(s.count) 条记录 · 最近 \(s.lastAt.formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                if !store.sessions.isEmpty {
                    Section {
                        Text("记录只存本机（App 私有目录），不上传。原始截图不落盘。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                // 全局人物：身份跨会话通用——同一个人在哪个群填过，所有群都认得
                if !store.knownPeople.isEmpty {
                    Section {
                        ForEach(store.knownPeople.prefix(20)) { p in
                            PersonRow(person: p, store: store)
                        }
                    } header: {
                        Text("人物（身份跨会话通用）")
                    } footer: {
                        Text("在这里填一次，所有会话都认得这个人。会话详情里还能补「在本群的角色」。")
                    }
                }
            }
            .navigationTitle("记录")
        }
    }
}

/// 全局人物的编辑行：填一次，所有会话都生效
struct PersonRow: View {
    let person: Person
    @ObservedObject var store: AnalysisStore
    @State private var draft: String = ""
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "person.crop.circle").font(.caption)
                Text(person.displayName).font(.subheadline).bold()
                Text("见过 \(person.seenCount) 次").font(.caption2).foregroundStyle(.secondary)
                if !person.identity.isEmpty {
                    Image(systemName: "checkmark.seal.fill").font(.caption2).foregroundStyle(.green)
                }
            }
            if !person.aliases.isEmpty {
                Text("另读到过：" + person.aliases.joined(separator: "、"))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            TextField("他是谁 / 什么角色（所有群通用）", text: $draft, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .font(.footnote)
            Button("保存（全局）") {
                store.setPersonIdentity(draft, name: person.displayName)
            }
            .font(.caption)
        }
        .padding(.vertical, 2)
        .onAppear {
            guard !loaded else { return }
            draft = person.identity
            loaded = true
        }
    }
}

/// 单个会话：档案编辑 + 该会话的全部分析记录
struct SessionDetailView: View {
    let sessionKey: String
    @ObservedObject var store: AnalysisStore

    @State private var relationshipDraft: String = ""
    @State private var noteDrafts: [String: String] = [:]
    @State private var loaded = false

    private var profile: SessionProfile? { store.profiles[sessionKey] }
    private var items: [StoredAnalysis] { store.analyses(in: sessionKey) }
    private var people: [String] { store.speakers(in: sessionKey) }

    var body: some View {
        List {
            // ---- 会话档案（注入判断层）----
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Text("这个会话是什么（会替代默认的关系描述）")
                        .font(.caption).foregroundStyle(.secondary)
                    TextField(profile?.isGroup == true ? "例：项目交流群，讨论实现与需求" : "例：同事，一起做项目",
                              text: $relationshipDraft, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .font(.footnote)
                        .onSubmit { store.setRelationship(relationshipDraft, key: sessionKey) }
                    Button("保存会话定位") {
                        store.setRelationship(relationshipDraft, key: sessionKey)
                    }
                    .font(.footnote)
                }
                .padding(.vertical, 2)
            } header: {
                Text("会话档案")
            } footer: {
                Text("把「这个场合是什么」写清楚能显著提升判断准确度——"
                     + "否则模型只能按默认的亲密关系假设去猜。")
            }

            // ---- 人物档案 ----
            if !people.isEmpty {
                Section("人物（每人一句身份描述）") {
                    ForEach(people, id: \.self) { name in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Image(systemName: "person.crop.circle").font(.caption)
                                Text(name).font(.subheadline).bold()
                            }
                            TextField("他是谁 / 什么角色",
                                      text: Binding(
                                        get: { noteDrafts[name] ?? profile?.notes[name] ?? "" },
                                        set: { noteDrafts[name] = $0 }),
                                      axis: .vertical)
                                .textFieldStyle(.roundedBorder)
                                .font(.footnote)
                            Button("保存 \(name)") {
                                store.setNote(noteDrafts[name] ?? "", person: name, key: sessionKey)
                            }
                            .font(.caption)
                        }
                        .padding(.vertical, 2)
                    }
                }
            }

            // ---- 分析记录 ----
            Section("分析记录（\(items.count) 条，新的在前）") {
                ForEach(items) { a in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(spacing: 6) {
                            Text(a.at.formatted(date: .omitted, time: .shortened))
                                .font(.caption2).foregroundStyle(.secondary)
                            Text("\(a.dangerLabel) \(String(format: "%.0f", a.danger))/9")
                                .font(.caption2).bold()
                                .foregroundStyle(a.danger < 3 ? .green : (a.danger < 6 ? .orange : .red))
                            Text(a.intentLabel).font(.caption2)
                            Text(String(format: "%.0f%%", a.intentConfidence * 100))
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        if let sp = a.speaker {
                            Text("来自 \(sp)").font(.caption2).foregroundStyle(.secondary)
                        }
                        Text(a.latestText).font(.subheadline)
                        if let ctx = a.contextLine {
                            Text("上文：\(ctx)").font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                        }
                        Text(a.actionAdvice).font(.caption)
                        // 语境透明度：模型以什么关系前提在判断
                        Text("档案 \(a.profileName) · 关系前提：\(a.relationshipUsed)")
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(3)
                        ForEach(a.contextNotes, id: \.self) { n in
                            Text("⚠ \(n)").font(.caption2).foregroundStyle(.orange)
                        }
                        ForEach(Array(a.candidates.enumerated()), id: \.offset) { i, c in
                            HStack(alignment: .top, spacing: 5) {
                                Text("#\(i + 1)").font(.caption2).foregroundStyle(.secondary)
                                Text(c).font(.caption)
                                if a.pickedIndex == i {
                                    Image(systemName: "checkmark.circle.fill")
                                        .font(.caption2).foregroundStyle(.green)
                                }
                            }
                        }
                    }
                    .padding(.vertical, 3)
                }
            }

            Section {
                Button("清空全部记录", role: .destructive) { store.clearAll() }
            }
        }
        .navigationTitle(profile?.title ?? "会话")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            guard !loaded else { return }
            relationshipDraft = profile?.relationship ?? ""
            noteDrafts = profile?.notes ?? [:]
            loaded = true
        }
    }
}
