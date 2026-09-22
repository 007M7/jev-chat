import SwiftUI

/// 主页面（实际使用）。
///
/// 刻意只留三块：**采集控制**、**最近一次分析结果**、折叠起来的**诊断**。
/// Gate 1 那套核对清单是验证期的东西，验证完就不该占着主界面——
/// 现在收进「诊断」折叠区，需要时展开还能用。
struct ProbeView: View {
    @ObservedObject var capture: CaptureController
    @ObservedObject var bridge: AppBridge
    @ObservedObject private var store = AnalysisStore.shared

    @State private var showSettings = false
    @State private var showDiagnostics = false
    /// 诊断里对 ①② 的观察结论（Gate 1 验证期用过，保留着）
    @State private var permissionAskedAgain: String = "未观察"
    @State private var indicatorSeen: String = "未观察"
    @State private var indicatorNote: String = ""

    var body: some View {
        NavigationStack {
            List {
                captureSection
                behaviorSection
                analysisSection
                diagnosticsSection
            }
            .navigationTitle("Jev")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: {
                        Label("设置", systemImage: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
        }
    }

    // MARK: 行为（用户自己决定读不读、读哪个）

    private var behaviorSection: some View {
        Section {
            Toggle("自动分析", isOn: $bridge.autoAnalyze)
            Toggle("快速模式（只出判断，不生成候选）", isOn: $bridge.fastMode)
            Toggle("安静模式（只在需要我回应或有风险时弹通知）", isOn: $bridge.quietNotifications)

            Picker("只跟随这个会话", selection: $store.followSessionKey) {
                Text("不限制（屏幕上是什么就读什么）").tag(String?.none)
                ForEach(store.sessions, id: \.key) { s in
                    Text(s.title).tag(String?.some(s.key))
                }
            }

            Button("用最近一帧立刻分析一次") {
                if let img = capture.lastFrame { bridge.analyzeNow(img) }
            }
            .disabled(capture.lastFrame == nil || bridge.isAnalyzing)
        } header: {
            Text("读什么")
        } footer: {
            Text("「只跟随这个会话」是给「我一边在微信聊天、一边又去用别的 App」这种情况准备的："
                 + "不定死目标时，屏幕上任何像聊天的界面（比如你在 QQ 里翻截图）都会被当成对话分析。")
        }
    }

    // MARK: 采集

    private var captureSection: some View {
        Section {
            LabeledContent("状态", value: capture.statusText)
            if !capture.isCapturing {
                Text("开始采集后切到微信正常聊天即可，结果会以通知形式弹出。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if capture.isCapturing {
                LabeledContent("已采集", value: "\(capture.totalFrames) 帧 · 最近 \(capture.lastFrameAgo)")
            }
            if let err = capture.lastError {
                Text(err).font(.footnote).foregroundStyle(.red)
            }
            if !capture.captureSupported {
                Text("此系统不支持采集（ScreenCaptureKit 需要 iOS 27）。升级后同一个包自动具备能力。")
                    .font(.footnote).foregroundStyle(.orange)
            }

            if capture.isCapturing {
                Button("停止采集", role: .destructive) { capture.stop() }
            } else {
                Button("开始采集") { capture.requestPermissionAndStart() }
                    .disabled(!capture.captureSupported)
            }
        }
    }

    // MARK: 最近一次分析

    private var analysisSection: some View {
        Section("最近一次分析") {
            if bridge.isAnalyzing {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("分析中…（一轮大约十几秒：视觉模型读屏幕最慢，约 4-7 秒）")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            Text(bridge.status).font(.footnote).foregroundStyle(.secondary)

            if let err = bridge.lastError {
                Text(err).font(.footnote).foregroundStyle(.red)
            }

            if let a = bridge.latest {
                VStack(alignment: .leading, spacing: 6) {
                    Text(a.chatTitle).font(.caption).foregroundStyle(.secondary)
                    Text(a.latestText.isEmpty ? "（无文本消息）" : a.latestText)
                        .font(.subheadline).bold()
                    if let sp = a.speaker {
                        Text("来自 \(sp)" + (a.contextLine.map { " · 上文：\($0)" } ?? ""))
                            .font(.caption).foregroundStyle(.secondary)
                    } else if let ctx = a.contextLine {
                        Text("上文：\(ctx)").font(.caption).foregroundStyle(.secondary)
                    }

                    HStack(spacing: 8) {
                        Text("\(a.dangerLabel) \(String(format: "%.0f", a.danger))/9")
                            .font(.caption).bold()
                            .foregroundStyle(a.danger < 3 ? .green : (a.danger < 6 ? .orange : .red))
                        Text(a.intentLabel).font(.caption).bold()
                        Text(String(format: "把握 %.0f%%", a.intentConfidence * 100))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    Text(a.actionAdvice).font(.footnote)

                    Divider()
                    ForEach(Array(a.candidates.enumerated()), id: \.offset) { i, c in
                        HStack(alignment: .top, spacing: 6) {
                            Text("#\(i + 1)").font(.caption2).foregroundStyle(.secondary)
                            Text(c).font(.footnote)
                        }
                    }
                    Text("候选已同时发到通知里；点通知上的按钮即可复制，回微信长按粘贴。")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
            } else if !bridge.isAnalyzing {
                Text("还没有分析结果。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: 诊断（折叠）

    private var diagnosticsSection: some View {
        Section {
            DisclosureGroup("诊断（验证期用的内容，平时不用看）", isExpanded: $showDiagnostics) {
                // 去重/过滤统计
                LabeledContent("已完成分析", value: "\(bridge.analysisCount) 次")
                LabeledContent("内容未变而跳过", value: "\(bridge.skippedAsRepeat) 次")
                LabeledContent("非聊天界面跳过", value: "\(bridge.skippedNotChat) 次")
                LabeledContent("自己通知污染跳过", value: "\(bridge.skippedAsEcho) 次")
                LabeledContent("不在跟随会话而跳过", value: "\(bridge.skippedNotTarget) 次")
                LabeledContent("变化检测门", value: capture.gateStatus)
                LabeledContent("放行去分析", value: "\(capture.stableFrameCount) 次 / 共 \(capture.totalFrames) 帧")
                LabeledContent("帧尺寸", value: capture.frameSize)

                Text("「内容未变而跳过」是内容级去重：像素会因噪声微变，但对话没变就不重复分析。"
                     + "「非聊天界面跳过」是感知没读到任何消息（在桌面或别的 App）时丢弃。")
                    .font(.caption2).foregroundStyle(.secondary)

                Button("弹一条带 3 个候选的测试通知") { bridge.selfTestOutput() }

                // Gate 1 的验收记录（已通过，留档）
                VStack(alignment: .leading, spacing: 6) {
                    Text("Gate 1 结论（已通过）").font(.caption).bold()
                    Text("① 授权：每次都重新弹 —— 使用上开一次就一直跑")
                        .font(.caption2).foregroundStyle(.secondary)
                    Text("② 录屏指示条：有（灵动岛位置）")
                        .font(.caption2).foregroundStyle(.secondary)
                    Text(capture.backgroundFrameSummary())
                        .font(.caption2)
                        .foregroundStyle(capture.backgroundFrameSummary().hasPrefix("✅") ? .green : .secondary)
                }
                .padding(.vertical, 2)

                Button("把最近一帧存到相册") { capture.saveLastFrameToPhotos() }
                    .disabled(capture.lastFrame == nil)
                Button("清空采集记录（重测用）", role: .destructive) { capture.resetLog() }
            }
        }
    }
}
