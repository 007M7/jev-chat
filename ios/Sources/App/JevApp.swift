import SwiftUI
import UIKit

@main
struct JevApp: App {
    @StateObject private var capture = CaptureController()
    @StateObject private var bridge = AppBridge()

    var body: some Scene {
        WindowGroup {
            ProbeView(capture: capture, bridge: bridge)
                .onAppear {
                    bridge.setUp()
                    // 把采集与流水线接起来：变化检测门判定"画面已稳定"时跑一次分析。
                    // 采集与限流都不需要感知层知道，这里只挂一个回调。
                    capture.onStableFrame = { [weak bridge] image in
                        bridge?.handleStableFrame(image)
                    }
                }
        }
    }
}

/// 把输出层、流水线、采集回调收在一起，避免视图里到处 import。
@MainActor
final class AppBridge: ObservableObject {
    let notifications = NotificationPresenter()
    let liveActivity = LiveActivityController()
    let pipeline = JevPipeline()

    /// 最近一次分析结果（界面展示用）
    @Published private(set) var latest: JevAnalysis?
    /// 流水线状态文案
    @Published private(set) var status: String = "等待稳定画面"
    @Published private(set) var isAnalyzing = false
    @Published private(set) var lastError: String?
    @Published private(set) var analysisCount = 0

    func setUp() {
        notifications.setUp()
    }

    /// 变化检测门放行一帧 → 跑一次完整分析 → 弹候选通知
    ///
    /// 这里刻意做成"来了就跑"，而不是等用户点按钮：感知+判断+起草合计 8~16 秒
    /// （实测），等用户看向面板时结果已经在了，比压模型延迟更实际。
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
                let a = try await pipeline.analyze(image: image)
                latest = a
                analysisCount += 1
                status = String(format: "分析完成 · 感知 %.1fs 总 %.1fs", a.perceptionSeconds, a.totalSeconds)

                // 结果走通知（3 个候选按钮）+ 灵动岛（这一版没有扩展，灵动岛不会显示）
                notifications.present(
                    candidates: a.candidates,
                    headline: "\(a.dangerLabel) \(String(format: "%.0f", a.danger))/9 · \(a.intentLabel) · \(a.actionAdvice)",
                    chatTitle: a.chatTitle
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
    /// 把"显示层能不能用"和"模型准不准"分开验——显示层的问题不该被模型问题掩盖。
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
