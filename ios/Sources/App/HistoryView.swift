import SwiftUI

/// 记录页：按会话（群聊 / 单聊）分组的分析历史 + 会话档案编辑。
///
/// 两个用途：
///   1. **可查的记录**——类似聊天记录，能回看每次分析的结论与候选
///   2. **人物 memory 的入口**——人工把"这个群是什么、这个人是谁"写清楚，
///      注入判断层。这是提升准确度最直接的一环：题目集原本要猜关系前提。
struct HistoryView: View {
    @ObservedObject private var store = AnalysisStore.shared

    static func shortTime(_ d: Date) -> String {
        let c = Calendar.current
        if c.isDateInToday(d) {
            return d.formatted(date: .omitted, time: .shortened)
        }
        if c.isDateInYesterday(d) { return "昨天" }
        return d.formatted(.dateTime.month().day())
    }
    /// 群聊和单聊分开看：群聊里"谁在跟谁说话"是一等公民，单聊没有这个问题。
    /// 混在一起列，群聊一多就把单聊淹了。
    private var groups: [ChatSession] { store.sessions.filter { $0.isGroup } }
    private var directs: [ChatSession] { store.sessions.filter { !$0.isGroup } }

    var body: some View {
        NavigationStack {
            List {
                if store.sessions.isEmpty {
                    Section {
                        Text("还没有记录。开始采集后，每次自动分析都会记在这里。")
                            .foregroundStyle(.secondary)
                    }
                }

                if !groups.isEmpty {
                    Section {
                        ForEach(groups, id: \.key) { s in
                            sessionRow(s)
                        }
                    } header: {
                        Label("群聊（\(groups.count)）", systemImage: "person.3.fill")
                    } footer: {
                        Text("群聊记录会逐条带发言人累积，判断时用的是这段上下文，不是单张截图里的最后一句。")
                    }
                }

                if !directs.isEmpty {
                    Section {
                        ForEach(directs, id: \.key) { s in
                            sessionRow(s)
                        }
                    } header: {
                        Label("单聊（\(directs.count)）", systemImage: "person.fill")
                    }
                }

                if !store.sessions.isEmpty {
                    Section {
                        Text("记录只存本机（App 私有目录），不上传。原始截图不落盘。"
                             + "人物身份在「联系人」页统一维护。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("记录")
        }
    }

    @ViewBuilder
    private func sessionRow(_ s: ChatSession) -> some View {
        NavigationLink {
            SessionDetailView(sessionKey: s.key, store: store)
        } label: {
            HStack(spacing: 11) {
                AvatarView(title: s.title, isGroup: s.isGroup)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(s.title).font(.subheadline).bold().lineLimit(1)
                        Spacer(minLength: 4)
                        Text(Self.shortTime(s.lastAt))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 5) {
                        if store.isFollowed(s.key) {
                            Label("跟随中", systemImage: "target")
                                .font(.caption2).bold().foregroundStyle(.blue)
                        }
                        Text(store.lastPreview(in: s.key))
                            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        Spacer(minLength: 4)
                        if store.profiles[s.key]?.notes.isEmpty == false {
                            Image(systemName: "checkmark.seal.fill")
                                .font(.caption2).foregroundStyle(.green)
                        }
                        Text("\(s.count)")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.vertical, 2)
        }
        .swipeActions(edge: .leading) {
            // 在会话列表里直接切换"只读这个会话"——比去主页面下拉选好用
            Button {
                store.toggleFollow(s.key)
            } label: {
                Label(store.isFollowed(s.key) ? "取消跟随" : "只读这个",
                      systemImage: store.isFollowed(s.key) ? "target.slash" : "target")
            }
            .tint(store.isFollowed(s.key) ? .gray : .blue)
        }
    }
}

/// 圆形头像，样式对齐聊天软件的联系人列表：取标题首字 + 由标题派生的固定配色。
/// 不显示真实头像（截图里那些头像是别人的肖像，没必要留存）。
struct AvatarView: View {
    let title: String
    let isGroup: Bool

    private static let palette: [Color] = [
        .blue, .green, .orange, .purple, .pink, .teal, .indigo, .brown,
    ]

    private var color: Color {
        var h = 5381
        for u in title.unicodeScalars { h = ((h << 5) &+ h) &+ Int(u.value) }
        return Self.palette[abs(h) % Self.palette.count]
    }

    private var initial: String {
        let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? "?" : String(t.prefix(1))
    }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(color.opacity(0.85))
                .frame(width: 44, height: 44)
            if isGroup {
                Text(initial).font(.title3).bold().foregroundStyle(.white)
                // 群里用角标提示是多人群聊
                Image(systemName: "person.3.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(.white)
                    .padding(3)
                    .background(Circle().fill(.black.opacity(0.25)))
                    .offset(x: 15, y: 15)
            } else {
                Text(initial).font(.title3).bold().foregroundStyle(.white)
            }
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
    @State private var showAllLines = false

    private var profile: SessionProfile? { store.profiles[sessionKey] }
    private var items: [StoredAnalysis] { store.analyses(in: sessionKey) }
    private var people: [String] { store.speakers(in: sessionKey) }
    private var isGroup: Bool { profile?.isGroup ?? items.first?.isGroup ?? false }

    /// 累积下来的对话流。这是"感知只读了一条消息"这个误解的正面回答：
    /// 每帧读到的消息会并进这里，判断层用的就是它。
    private var transcript: [ChatLine] { store.conversation(in: sessionKey) }
    private var shownLines: [ChatLine] {
        showAllLines ? transcript : Array(transcript.suffix(60))
    }
    private var transcriptTitle: String { isGroup ? "群聊记录" : "聊天记录" }

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

            // ---- 群聊记录（累积的对话，逐条带发言人）----
            Section {
                if transcript.isEmpty {
                    Text("还没有累积到对话。每次分析都会把那一屏读到的消息并进来，"
                         + "同一个会话只留一份（重叠的部分自动去重）。")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    ForEach(shownLines) { line in
                        HStack(alignment: .top, spacing: 7) {
                            if line.side == "me" { Spacer(minLength: 30) }
                            VStack(alignment: line.side == "me" ? .trailing : .leading,
                                   spacing: 2) {
                                HStack(spacing: 5) {
                                    if line.side != "me", let sd = line.sender, !sd.isEmpty {
                                        Text(sd).font(.caption2).bold()
                                    }
                                    Text(line.at.formatted(date: .omitted, time: .shortened))
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                Text(line.text)
                                    .font(.footnote)
                                    .padding(.horizontal, 8).padding(.vertical, 5)
                                    .background(
                                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                                            .fill(line.side == "me"
                                                  ? Color.accentColor.opacity(0.18)
                                                  : Color.gray.opacity(0.14)))
                            }
                            if line.side != "me" { Spacer(minLength: 30) }
                        }
                        .padding(.vertical, 1)
                    }
                    if transcript.count > shownLines.count {
                        Button("显示全部 \(transcript.count) 条") { showAllLines = true }
                            .font(.caption)
                    }
                }
            } header: {
                Label("\(transcriptTitle)（\(transcript.count) 条）",
                      systemImage: "bubble.left.and.bubble.right")
            } footer: {
                Text("这是判断时真正喂给模型的上下文（最多 \(JevPipeline.maxMessagesInState) 条），"
                     + "不是单张截图的最后一句。左侧气泡是别人说的，右侧是我说的。")
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
                        // 逐条核对耗时用——界面上那行小字是静态预估，容易被误读成实测
                        HStack(spacing: 8) {
                            Text(String(format: "感知 %.1fs", a.perceptionSeconds))
                            Text(String(format: "总 %.1fs", a.totalSeconds))
                            if a.fastMode { Text("快速模式") }
                        }
                        .font(.caption2).foregroundStyle(.secondary)
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
