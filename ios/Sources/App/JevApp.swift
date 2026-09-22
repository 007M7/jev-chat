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

    func setUp() {
        // 通知的注册已经在 AppDelegate 里做了；这里只补一次以免首帧竞态
        notifications.register()
    }

    /// 变化检测门放行一帧 → 跑一次完整分析 → 弹候选通知 → 记入本地记录
    ///
    /// 这里刻意做成"来了就跑"，而不是等用户点按钮：感知+判断+起草合计 8~16 秒
    /// （实测感知 7s、总 13.8s），等用户看向面板时结果已经在了，
    /// 比压模型延迟更实际。
    func handleStableFrame(_ image: UIImage) {
        guard !isAnalyzing else { return }          // 上一次还没跑完就跳过，避免堆积
        guard !BrainConfig.allKeyNames().isEmpty else {
            status = "还没配密钥，去设置里填"
            return
        }
        isAnalyzing = true
        lastError = nil
        status = "分析中…"

        Task {
            do {
                // 会话档案在**感知拿到标题之后**才取（见 pipeline 里的说明）：
                // 用上一次的标题猜档案会把上一个会话的身份注入进来，那比不注入更糟。
                let a = try await pipeline.analyze(image: image) { [weak self] title, isGroup in
                    guard let self else { return ("", nil) }
                    let key = self.store.sessionKey(for: title, isGroup: isGroup)
                    guard let p = self.store.profiles[key] else { return ("", nil) }

                    // 人工填的关系与人物身份 → 注入判断层
                    let rel = p.relationshipText(fallback: "")
                    var mem: [String: Any]? = nil
                    if !p.notes.isEmpty {
                        mem = [
                            "note": "以下是人工维护的会话与人物档案，属于已知前提，不是当前对话内容",
                            "chat_title": p.title,
                            "is_group": p.isGroup,
                            "facts": ["人工档案": p.notes.map { "\($0.key)：\($0.value)" }.sorted()],
                        ]
                    }
                    return (rel, mem)
                }
                latest = a
                analysisCount += 1
                status = String(format: "分析完成 · 感知 %.1fs 总 %.1fs", a.perceptionSeconds, a.totalSeconds)

                // 记入本地记录（按会话分组，App 内可查）
                let key = store.sessionKey(for: a.chatTitle, isGroup: a.isGroup)
                let rec = StoredAnalysis(
                    sessionKey: key, chatTitle: a.chatTitle, isGroup: a.isGroup,
                    speaker: a.speaker, latestText: a.latestText, contextLine: a.contextLine,
                    danger: a.danger, dangerLabel: a.dangerLabel,
                    intentLabel: a.intentLabel, intentConfidence: a.intentConfidence,
                    actionAdvice: a.actionAdvice, candidates: a.candidates, pickedIndex: nil
                )
                store.add(rec)

                // 结果走通知（3 个候选按钮）
                let dangerText = String(format: "%.0f", a.danger)
                let headline = "\(a.dangerLabel) \(dangerText)/9 · \(a.intentLabel) · \(a.actionAdvice)"
                notifications.present(
                    candidates: a.candidates,
                    headline: headline,
                    chatTitle: a.chatTitle,
                    analysisID: rec.id
                )
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
            isAnalyzing = false
        }
    }

    /// 用界面里"最近一帧"手动跑一次（不依赖门放行），方便调试
    func analyzeNow(_ image: UIImage) {
        handleStableFrame(image)
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
