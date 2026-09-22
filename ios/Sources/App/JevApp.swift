import SwiftUI
import UIKit

@main
struct JevApp: App {
    /// 通知代理必须在启动时就注册：被"后台拉起投递通知动作"时 SwiftUI 的 .onAppear 不会执行
    @UIApplicationDelegateAdaptor(JevAppDelegate.self) private var appDelegate

    @StateObject private var capture = CaptureController()
    @StateObject private var bridge = AppBridge()

    var body: some Scene {
        WindowGroup {
            TabView {
                ProbeView(capture: capture, bridge: bridge)
                    .tabItem { Label("探针", systemImage: "waveform.path.ecg") }
                HistoryView()
                    .tabItem { Label("记录", systemImage: "clock.arrow.circlepath") }
            }
            .onAppear {
                bridge.setUp()
                // 把采集与流水线接起来：变化检测门判定"画面已稳定"时跑一次分析。
                capture.onStableFrame = { [weak bridge] image in
                    bridge?.handleStableFrame(image)
                }
            }
        }
    }
}

/// 把输出层、流水线、记录存储收在一起，避免视图里到处 import。
@MainActor
final class AppBridge: ObservableObject {
    // 与 AppDelegate 里注册的是同一个实例（通知代理必须是它）
    let notifications = NotificationPresenter.shared
    let liveActivity = LiveActivityController()
    let pipeline = JevPipeline()
    let store = AnalysisStore.shared

    @Published private(set) var latest: JevAnalysis?
    @Published private(set) var status: String = "等待稳定画面"
    @Published private(set) var isAnalyzing = false
    @Published private(set) var lastError: String?
    @Published private(set) var analysisCount = 0
    @Published private(set) var skippedAsEcho = 0

    /// 刚弹过通知的冷却期。
    /// **通知横幅会浮在微信上方**，若这段时间又抓一帧，我们自己的横幅会被当成聊天内容读进去
    /// （实测踩过：标题被读成「Jev · Jev · Jev · 群名」，候选回复被当成"对方最新消息"）。
    /// 横幅大约 5 秒自动消失，这里留 10 秒余量。
    private var suppressUntil: Date?
    /// 最近弹过的候选，用于回声检测
    private var recentCandidates: [String] = []
    /// 上一个成功识别的会话（标题 + 是否群聊）。
    /// 用来兜住"这一帧读不到标题/判断不出群聊"——否则会话键会变成「未知会话」，
    /// 人工填的关系与人物档案全部失效，还会退回到错误的关系前提。
    private var lastSession: JevPipeline.SessionHint?
    /// 每个会话上一次分析过的消息签名：**内容级去重**。
    /// 像素会因噪声（光标、亮度、渲染差）微变，但"对话没变"就不该重复分析——
    /// 实测静默状态下会每分钟触发好几次，就是只做了像素级比对。
    /// 每个会话**上一次分析过的最新消息**（发言人 + 文本）——真正的"有新消息才分析"。
    ///
    /// 为什么不用整段窗口的签名：那个判据不稳。群聊里消息滚动、截图边界变化、
    /// 感知偶尔多读少读一条，签名就变了——即使最新那条消息根本没变也会重跑。
    /// 实测：同一句「现在流行这样测智商」在同一分钟内被分析了两次，
    /// 两条记录的发言人、"上文"完全一样。
    /// Jev 判断的对象就是最新消息，所以去重判据就该是它。
    private var lastLatest: [String: String] = [:]
    /// 两次分析之间的最小间隔，兜住像素噪声造成的连续触发
    private var lastAnalysisAt: Date?
    @Published private(set) var skippedAsRepeat = 0
    @Published private(set) var skippedNotChat = 0
    @Published private(set) var skippedNotTarget = 0

    /// 自动分析开关。关掉后只有手动点"立刻分析一次"才会跑。
    /// 用户要求：不要无条件隔十几秒就读一次——由他决定这个功能开不开。
    // 注意：非可选类型 + didSet 观察器**必须给初始值**，否则 Swift 报
    // "class 'AppBridge' has no initializers"（云构建实测踩到）。
    // 可选类型（如 followSessionKey）自动默认 nil，不受影响。
    @Published var autoAnalyze: Bool = true {
        didSet { UserDefaults.standard.set(autoAnalyze, forKey: Self.kAuto) }
    }

    /// 快速模式：只出判断，不生成候选。实测能省掉约一半耗时（起草+排序占总耗时约一半）。
    @Published var fastMode: Bool = false {
        didSet { UserDefaults.standard.set(fastMode, forKey: Self.kFast) }
    }
    /// 通知策略：true = 只在"需要我回应"或"有风险"时才弹通知。
    ///
    /// 为什么需要：500 人的活跃群里每条消息都会触发分析，全都弹通知会把人淹了。
    /// 记录仍然全部存下来（记录页可查），只是不打扰。
    @Published var quietNotifications: Bool = true {
        didSet { UserDefaults.standard.set(quietNotifications, forKey: Self.kQuiet) }
    }

    private static let kAuto = "jev_auto_analyze"
    private static let kFast = "jev_fast_mode"
    private static let kQuiet = "jev_quiet_notifications"
    /// 分析中的计时器：界面上显示"已 N 秒"，让"慢"变成可观察的数字而不是感觉
    private var progressTimer: Timer?
    private var analysisStartedAt: Date?

    func setUp() {
        // 通知的注册已经在 AppDelegate 里做了；这里只补一次以免首帧竞态
        notifications.register()
        // 读回用户上次的选择（默认：自动开、不限制会话）
        if UserDefaults.standard.object(forKey: Self.kAuto) != nil {
            autoAnalyze = UserDefaults.standard.bool(forKey: Self.kAuto)
        }
        if UserDefaults.standard.object(forKey: Self.kFast) != nil {
            fastMode = UserDefaults.standard.bool(forKey: Self.kFast)
        }
        if UserDefaults.standard.object(forKey: Self.kQuiet) != nil {
            quietNotifications = UserDefaults.standard.bool(forKey: Self.kQuiet)
        }
    }

    /// 变化检测门放行一帧 → 跑一次完整分析 → 弹候选通知 → 记入本地记录
    ///
    /// 这里刻意做成"来了就跑"，而不是等用户点按钮：感知+判断+起草合计 8~16 秒
    /// （实测感知 7s、总 13.8s），等用户看向面板时结果已经在了，
    /// 比压模型延迟更实际。
    func handleStableFrame(_ image: UIImage, force: Bool = false) {
        guard !isAnalyzing else { return }          // 上一次还没跑完就跳过，避免堆积
        guard !BrainConfig.allKeyNames().isEmpty else {
            status = "还没配密钥，去设置里填"
            return
        }
        // 自动分析关着时，只有手动触发才跑（用户要求"由我决定这个功能开不开"）
        if !force && !autoAnalyze {
            status = "自动分析已关闭（可在上面打开，或手动分析一次）"
            return
        }
        // 冷却期：刚弹过通知就别抓，否则会把自己的横幅读成聊天内容
        if let s = suppressUntil, Date() < s {
            status = String(format: "刚弹过通知，等横幅消失（%.0fs）", max(0, s.timeIntervalSinceNow))
            return
        }
        // 最小间隔：像素噪声（光标、亮度、渲染差）会让门在静默时反复放行，这里兜一层
        // 正确性由"最新消息是否变化"保证，这里只防抖动。
        // 从 12 秒降到 3 秒——否则真的来了新消息（间隔 5 秒）会被这个门挡掉。
        let minInterval: TimeInterval = 3
        if let t = lastAnalysisAt, Date().timeIntervalSince(t) < minInterval {
            return
        }
        isAnalyzing = true
        lastError = nil
        analysisStartedAt = Date()
        status = "分析中… 0s"
        startProgressTimer()

        Task {
            do {
                // 会话档案在**感知拿到标题之后**才取（见 pipeline 里的说明）：
                // 用上一次的标题猜档案会把上一个会话的身份注入进来，那比不注入更糟。
                let a = try await pipeline.analyze(image: image, sessionHint: lastSession, skipDraft: fastMode) { [weak self] title, isGroup in
                    guard let self else { return ("", nil) }
                    let key = self.store.sessionKey(for: title, isGroup: isGroup)

                    // 人工填的关系与人物身份 → 注入判断层。
                    // 人物身份是**跨会话**的：同一个人在其他群填过，这里也会带上。
                    let rel = self.store.profiles[key]?.relationshipText(fallback: "") ?? ""
                    let facts = self.store.personFacts(inSession: key)
                    var mem: [String: Any]? = nil
                    if !facts.isEmpty {
                        mem = [
                            "note": "以下是人工维护的会话与人物档案（人物身份跨会话通用），"
                                  + "属于已知前提，不是当前对话内容",
                            "chat_title": title,
                            "is_group": isGroup,
                            "facts": ["人工档案": facts],
                        ]
                    }
                    return (rel, mem)
                }
                latest = a
                analysisCount += 1
                status = String(format: "分析完成 · 感知 %.1fs 总 %.1fs", a.perceptionSeconds, a.totalSeconds)

                // 1) 不是聊天界面：感知没读到任何消息（在桌面、别的 App、聊天列表页……）
                if a.messageCount == 0 {
                    skippedNotChat += 1
                    status = "这一帧没读到对话（不是聊天界面？），已跳过"
                    finishAnalysis()
                    return
                }

                // 2) **不是我要跟的那个会话**：用户在别的 App / 别的聊天里时不该产出结果。
                //    实测踩过——用 QQ 发截图时，QQ 界面被当成群聊分析了三次。
                let skey = store.sessionKey(for: a.chatTitle, isGroup: a.isGroup)
                if !force, let target = store.followSessionKey, target != skey {
                    skippedNotTarget += 1
                    status = "当前不在跟随的会话里（\(a.chatTitle)），已跳过"
                    finishAnalysis()
                    return
                }

                // 3) **没有新消息**：最新消息与上次分析的那条相同（发言人 + 文本）。
                //    手动触发（force）时跳过这道判断——用户明确要求分析就该给结果。
                //    "没读到对话"与"读到自己的通知"仍是硬拦截，因为那种结果本身就是错的。
                //    判据刻意只看最新一条：Jev 判断的对象就是它，它没变就没有新东西可判。
                //    用整段窗口做判据会抖动（消息滚动、截图边界、感知多读少读一条），
                //    实测导致同一句话被重复分析。
                let latestKey = "\(a.speaker ?? "")|\(a.latestText)"
                if !force, let prev = lastLatest[skey], prev == latestKey {
                    skippedAsRepeat += 1
                    status = "最新消息没有变化，已跳过（不是定时重复分析）"
                    finishAnalysis()
                    return
                }

                // 4) 回声：最新消息就是自己刚弹的候选（notification 横幅被读进来）
                if JevPipeline.looksLikeOurEcho(a.latestText, recentCandidates: recentCandidates) {
                    skippedAsEcho += 1
                    status = "这一帧读到了自己的通知，已丢弃"
                    finishAnalysis()
                    return
                }

                lastLatest[skey] = latestKey
                lastAnalysisAt = Date()

                // 记入本地记录（按会话分组，App 内可查）
                let key = skey
                let rec = StoredAnalysis(
                    sessionKey: key, chatTitle: a.chatTitle, isGroup: a.isGroup,
                    speaker: a.speaker, latestText: a.latestText, contextLine: a.contextLine,
                    danger: a.danger, dangerLabel: a.dangerLabel,
                    intentLabel: a.intentLabel, intentConfidence: a.intentConfidence,
                    actionAdvice: a.actionAdvice, candidates: a.candidates, pickedIndex: nil,
                    relationshipUsed: a.relationshipUsed, contextNotes: a.contextNotes,
                    profileName: a.profileName, calibrated: a.calibrated,
                    perceptionSeconds: a.perceptionSeconds, totalSeconds: a.totalSeconds,
                    fastMode: fastMode, transcript: a.transcript,
                    answersSummary: a.answersSummary
                )
                store.add(rec)

                // 记住这次的会话身份，供下一帧兜底（标题被通知横幅盖住时特别有用）
                if a.chatTitle != "未知会话" {
                    lastSession = JevPipeline.SessionHint(title: a.chatTitle, isGroup: a.isGroup)
                }

                // 结果走通知。有候选就带 3 个按钮；快速模式下没有候选，只弹结论。
                let dangerText = String(format: "%.0f", a.danger)
                let headline = "\(a.dangerLabel) \(dangerText)/9 · \(a.intentLabel) · \(a.actionAdvice)"

                // 安静模式：只在"需要我回应"或"有风险"时才打扰（记录照存，不打断）
                let needReply = a.shouldReplyNow >= 0.5
                let risky = a.danger >= 4
                if quietNotifications && !needReply && !risky && !force {
                    status += "（不值得打扰，只记录不弹通知）"
                    lastLatest[skey] = latestKey
                    lastAnalysisAt = Date()
                    finishAnalysis()
                    return
                }
                if a.candidates.isEmpty {
                    notifications.presentVerdict(headline: headline, chatTitle: a.chatTitle)
                } else {
                    notifications.present(
                        candidates: a.candidates,
                        headline: headline,
                        chatTitle: a.chatTitle,
                        analysisID: rec.id
                    )
                }
                // 记住了这次弹了什么：接下来的 10 秒不抓帧，同时用于回声检测
                recentCandidates = a.candidates
                suppressUntil = Date().addingTimeInterval(10)
                liveActivity.start(chatTitle: a.chatTitle)
                liveActivity.update(danger: a.danger,
                                    headline: "\(a.intentLabel) · \(a.actionAdvice)",
                                    bestReply: a.candidates.first ?? "",
                                    bestScore: 0,
                                    hasCandidates: true,
                                    chatTitle: a.chatTitle)
            } catch {
                lastError = error.localizedDescription
                status = "分析失败"
            }
            finishAnalysis()
        }
    }

    /// 收口的结束动作：停表 + 复位标志。所有跳过路径都必须走它，
    /// 否则计时器会继续空转（状态栏一直涨秒数，但什么都没在跑）。
    private func finishAnalysis() {
        stopProgressTimer()
        isAnalyzing = false
    }

    private func startProgressTimer() {
        progressTimer?.invalidate()
        progressTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let t0 = self.analysisStartedAt else { return }
                self.status = String(format: "分析中… 已 %.0f 秒", Date().timeIntervalSince(t0))
            }
        }
    }

    private func stopProgressTimer() {
        progressTimer?.invalidate()
        progressTimer = nil
    }

    /// 用界面里"最近一帧"手动跑一次（不依赖门放行、也不受自动开关限制），方便调试
    func analyzeNow(_ image: UIImage) {
        handleStableFrame(image, force: true)
    }

    /// 输出通道自测：不跑模型，直接弹一条假候选。
    /// 把"显示层能不能用"和"模型准不准"分开验。
    func selfTestOutput() {
        let demo = ["我这就去处理，稍等十分钟", "收到，我今天下午给你", "哈哈好，听你的"]
        notifications.present(candidates: demo,
                              headline: "自测 · 对方想要具体行动 · 危险 2/9",
                              chatTitle: "输出通道自测")
        liveActivity.start(chatTitle: "输出通道自测")
        liveActivity.update(danger: 2,
                            headline: "对方想要具体行动",
                            bestReply: demo[0],
                            bestScore: 0.72,
                            hasCandidates: true,
                            chatTitle: "输出通道自测")
    }
}
