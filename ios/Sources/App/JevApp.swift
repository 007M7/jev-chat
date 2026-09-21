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
                }
        }
    }
}

/// 把输出层和采集层收在一起，避免视图里到处 import。
@MainActor
final class AppBridge: ObservableObject {
    let notifications = NotificationPresenter()
    let liveActivity = LiveActivityController()

    func setUp() {
        notifications.setUp()
    }

    /// 为验证输出通道单独准备的自测：不跑模型，直接弹一条假候选。
    /// 目的是把"显示层能不能用"和"模型准不准"分开验证——
    /// 显示层的问题（通知权限、按钮、灵动岛）不该被模型问题掩盖。
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
