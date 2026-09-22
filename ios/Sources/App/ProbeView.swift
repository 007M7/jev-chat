import SwiftUI

/// Gate 1 的核对界面。
///
/// 这一版**不做业务**（还不到时候）。它存在的唯一目的是回答三个未知点，
/// 它们任一失败，"全自动读屏"这条路线就要改设计：
///   ① 采集授权是否每次启动/每次开会话都要重新确认？
///   ② 顶部/灵动岛是否有录屏指示条常驻？
///   ③ 免费签名能不能带 `screen-capture` 后台模式（切到微信后帧还来不来）？
///
/// ①② 只能靠人看屏幕回答，所以界面上直接列出问题；③ 由后台帧计数自动回答。
struct ProbeView: View {
    @ObservedObject var capture: CaptureController
    @ObservedObject var bridge: AppBridge

    /// 用户对 ①② 的观察结论，写在这里便于截图回报
    @State private var permissionAskedAgain: String = "未观察"
    @State private var indicatorSeen: String = "未观察"
    @State private var indicatorNote: String = ""
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            List {
                captureSection
                analysisSection
                gateOneSection
                outputSection
                aboutSection
            }
            .navigationTitle("Jev 采集探针")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Label("设置", systemImage: "gearshape")
                    }
                }
            }
            .sheet(isPresented: $showSettings) {
                SettingsView()
            }
        }
    }

    // MARK: 分析（感知 → 判断 → 起草 → 排序）

    private var analysisSection: some View {
        Section("分析") {
            LabeledContent("流水线", value: bridge.status)
            LabeledContent("已完成分析", value: "\(bridge.analysisCount) 次")
            if bridge.isAnalyzing {
                HStack(spacing: 8) {
                    ProgressView()
                    Text("感知约 4~6 秒，判断约 1 秒，起草 2~7 秒").font(.footnote).foregroundStyle(.secondary)
                }
            }
            if let err = bridge.lastError {
                Text(err).font(.footnote).foregroundStyle(.red)
            }

            if let a = bridge.latest {
                // 与桌面版面板同构的信息层次：发言人 + 最新消息 + 上文 + 风险 + 建议 + 候选
                VStack(alignment: .leading, spacing: 6) {
                    Text(a.chatTitle).font(.caption).foregroundStyle(.secondary)
                    Text(a.latestText).font(.subheadline).bold()
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

                    if !a.calibrated {
                        Text("⚠ 档案 \(a.profileName) 尚未校准，结论仅供参考")
                            .font(.caption2).foregroundStyle(.orange)
                    }

                    Divider()
                    Text("候选回复（按合适度排序）").font(.caption).foregroundStyle(.secondary)
                    ForEach(Array(a.candidates.enumerated()), id: \.offset) { i, c in
                        HStack(alignment: .top, spacing: 6) {
                            Text("#\(i + 1)").font(.caption2).foregroundStyle(.secondary)
                            Text(c).font(.footnote)
                        }
                    }
                }
                .padding(.vertical, 4)
            }

            Button("用最近一帧立刻分析一次") {
                if let img = capture.lastFrame { bridge.analyzeNow(img) }
            }
            .disabled(capture.lastFrame == nil || bridge.isAnalyzing)

            Text("正常流程不用点这个：采集到稳定画面会自动分析，结果以通知形式弹出（3 个候选按钮）。")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }

    // MARK: 采集

    private var captureSection: some View {
        Section("采集") {
            LabeledContent("状态", value: capture.statusText)
            LabeledContent("累计帧数", value: "\(capture.totalFrames)")
            LabeledContent("最近一帧", value: capture.lastFrameAgo)
            LabeledContent("帧尺寸", value: capture.frameSize)
            if let err = capture.lastError {
                Text(err).font(.footnote).foregroundStyle(.red)
            }

            // 成本控制的可视证据：门放行次数应该远小于总帧数
            LabeledContent("变化检测门", value: capture.gateStatus)
            LabeledContent("放行去分析", value: "\(capture.stableFrameCount) 次 / 共 \(capture.totalFrames) 帧")
            Text("门的意义：云端感知一次约 4~6 秒且要花钱，绝不能每帧都调。"
                 + "只有画面稳定且与上次不同时才放行。放行次数远小于帧数就说明闸门在工作。")
                .font(.footnote).foregroundStyle(.secondary)

            if !capture.captureSupported {
                // iOS 26 等旧系统的真实情况：App 能装能跑，但采集能力不存在
                // （ScreenCaptureKit 的 iOS 版从 27.0 才有）。如实告知，不要静默失败。
                VStack(alignment: .leading, spacing: 6) {
                    Text("此系统不支持采集").font(.subheadline).bold().foregroundStyle(.orange)
                    Text("ScreenCaptureKit 的 iOS 版从 iOS 27 才有。当前系统是 iOS "
                         + UIDevice.current.systemVersion
                         + "。升级到 iOS 27 后同一个包会自动获得采集能力，不需要重新安装。")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text("通知横幅与灵动岛仍然可以测（下面「输出通道自测」）。")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
            }

            if capture.isCapturing {
                Button("停止采集", role: .destructive) { capture.stop() }
            } else {
                Button("开始采集（会弹出系统选择器）") { capture.requestPermissionAndStart() }
                    .disabled(!capture.captureSupported)
            }
            Button("把最近一帧存到相册（用来核对截到了什么）") {
                capture.saveLastFrameToPhotos()
            }
            .disabled(capture.lastFrame == nil)

            Text("提示：点开始后，系统会弹出内容选择器，请选「整个屏幕」。")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }

    // MARK: Gate 1

    private var gateOneSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 6) {
                Text("① 授权是否每次都要重新确认？").font(.subheadline).bold()
                Text("第二次点「开始采集」时，系统选择器是又弹出来要你选一遍，还是直接就开始采集了？")
                    .font(.footnote).foregroundStyle(.secondary)
                Picker("", selection: $permissionAskedAgain) {
                    Text("未观察").tag("未观察")
                    Text("每次都重新弹").tag("每次都重新弹")
                    Text("只弹了第一次").tag("只弹了第一次")
                }
                .pickerStyle(.segmented)
            }
            .padding(.vertical, 4)

            VStack(alignment: .leading, spacing: 6) {
                Text("② 有没有录屏指示条？").font(.subheadline).bold()
                Text("采集期间看屏幕顶部（灵动岛位置）：有没有常驻的红/蓝指示条或录屏标志？")
                    .font(.footnote).foregroundStyle(.secondary)
                Picker("", selection: $indicatorSeen) {
                    Text("未观察").tag("未观察")
                    Text("有，很明显").tag("有，很明显")
                    Text("有但不明显").tag("有但不明显")
                    Text("没有").tag("没有")
                }
                .pickerStyle(.segmented)
                TextField("补充说明（颜色/位置/是否闪烁）", text: $indicatorNote)
                    .textFieldStyle(.roundedBorder)
            }
            .padding(.vertical, 4)

            VStack(alignment: .leading, spacing: 6) {
                Text("③ 切到微信后还在收帧吗？（自动判断）").font(.subheadline).bold()
                Text(capture.backgroundFrameSummary())
                    .font(.footnote)
                    .foregroundStyle(capture.backgroundFrameSummary().hasPrefix("✅") ? .green :
                                     capture.backgroundFrameSummary().hasPrefix("❌") ? .red : .secondary)
                Text("测法：开始采集 → 按 Home 或切到微信 → 在微信里停 30 秒以上并滑动聊天 → 回本 App 看这行。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)

            Button("清空记录（重新测一遍）", role: .destructive) { capture.resetLog() }
        } header: {
            Text("Gate 1 核对清单")
        } footer: {
            Text("三个都通过才值得投入业务代码；③ 若显示「后台收不到帧」，全自动路线要改成截图触发。")
        }
    }

    // MARK: 输出通道自测

    private var outputSection: some View {
        Section("输出通道自测（不依赖模型）") {
            Button("弹一条带 3 个候选的通知") { bridge.selfTestOutput() }
            Text("点通知上的候选按钮后，该候选会进剪贴板（不发送），回微信长按即可粘贴。"
                 + "这一条同时验了「绝不自动发送」的红线：App 没有往别的 App 输入框写文本的能力。")
                .font(.footnote).foregroundStyle(.secondary)
            LabeledContent("实时活动可用", value: bridge.liveActivity.isAvailable ? "是" : "否（系统设置里关了？）")
        }
    }

    private var aboutSection: some View {
        Section("说明") {
            Text("这一版只验证采集与显示，不跑判断模型。")
            Text("路线背景：iOS 没有跨 App 悬浮窗，所以结果走「通知横幅（可点选）+ 灵动岛实时活动」两个系统通道；"
                 + "真正的悬浮窗在 iOS 上不存在，这是系统限制不是实现难度。")
                .font(.footnote).foregroundStyle(.secondary)
        }
    }
}
