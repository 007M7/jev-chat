import Foundation
import ActivityKit

/// 灵动岛/锁屏实时活动的数据结构。
///
/// 放在 Shared/ 下是因为 **App 与 widget 扩展都要编译这个文件**：App 负责更新内容，
/// 扩展负责渲染。两边必须用同一个类型定义，否则 ContentState 对不上。
///
/// 为什么用它：iOS 上没有跨 App 悬浮窗，但实时活动是系统认可的、能常驻在灵动岛上
/// 且不遮挡聊天内容的显示面。它是最接近安卓悬浮窗体验的替代品。
struct JevActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        /// 危险等级 0~9，用来决定配色
        var danger: Double
        /// 一句话结论，例如「对方要具体行动」
        var headline: String
        /// 最佳候选回复（灵动岛空间小，只放这一条）
        var bestReply: String
        /// 该候选被 Jev 打的分，0~1
        var bestScore: Double
        /// 是否有待处理的候选（用来提示"去看通知"）
        var hasCandidates: Bool
        /// 会话名，比如「小美」
        var chatTitle: String
    }

    /// 活动开始后不变的部分
    var startedAt: Date
}
