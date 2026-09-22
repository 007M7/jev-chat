import Foundation
import ActivityKit

/// 结果输出的第二个通道：**灵动岛 / 锁屏实时活动**（用户已确认可行）。
///
/// 它和通知的分工：
///   - 通知横幅：能承载 3 个候选按钮的**交互**，但会消失
///   - 实时活动：**常驻**显示危险等级和最佳候选，不遮挡聊天内容，但不能点击选择
/// 两个一起用才接近安卓悬浮窗的体验。
///
/// 实时活动的界面必须由 widget 扩展渲染（ActivityKit 的硬要求），
/// 所以 Shared/JevActivityAttributes.swift 同时被 App 和扩展编译。
@MainActor
final class LiveActivityController {

    private var activity: Activity<JevActivityAttributes>?

    /// 实时活动能否用：用户可能在设置里关了，或者设备不支持
    var isAvailable: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    func start(chatTitle: String) {
        guard isAvailable else {
            NSLog("[Jev] 实时活动不可用（用户关闭或设备不支持）")
            return
        }
        guard activity == nil else { return }
        let attrs = JevActivityAttributes(startedAt: Date())
        let state = JevActivityAttributes.ContentState(
            danger: 0,
            headline: "等待对方消息",
            bestReply: "",
            bestScore: 0,
            hasCandidates: false,
            chatTitle: chatTitle
        )
        do {
            activity = try Activity.request(
                attributes: attrs,
                content: .init(state: state, staleDate: nil)
            )
        } catch {
            NSLog("[Jev] 启动实时活动失败: \(error.localizedDescription)")
        }
    }

    func update(danger: Double, headline: String, bestReply: String,
                bestScore: Double, hasCandidates: Bool, chatTitle: String) {
        guard let activity else { return }
        let state = JevActivityAttributes.ContentState(
            danger: danger,
            headline: headline,
            bestReply: bestReply,
            bestScore: bestScore,
            hasCandidates: hasCandidates,
            chatTitle: chatTitle
        )
        Task {
            await activity.update(.init(state: state, staleDate: nil))
        }
    }

    func end() {
        guard let activity else { return }
        Task {
            await activity.end(nil, dismissalPolicy: .immediate)
            self.activity = nil
        }
    }
}
