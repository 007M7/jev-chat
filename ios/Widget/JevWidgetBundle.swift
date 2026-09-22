import ActivityKit
import SwiftUI
import WidgetKit

/// 灵动岛 / 锁屏实时活动的渲染端。
///
/// ActivityKit 要求实时活动的界面由 widget 扩展提供，App 只能更新数据、不能自己画。
/// 这是本 target 存在的唯一原因。
///
/// 分工（两个通道一起用才接近安卓悬浮窗的体验）：
///   - 实时活动（这里）：常驻、不遮挡、能显示危险等级和最佳候选，但**不能点击选择**
///   - 通知横幅：能承载 3 个候选按钮的交互，但会消失
@main
struct JevWidgetBundle: WidgetBundle {
    var body: some Widget {
        JevLiveActivity()
    }
}

struct JevLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: JevActivityAttributes.self) { context in
            // 锁屏 / 长时间显示的样子：空间大，信息给全
            LockScreenView(state: context.state)
                .activityBackgroundTint(Color.black.opacity(0.55))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    DangerBadge(danger: context.state.danger)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.chatTitle)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(context.state.headline.isEmpty ? "等待对方消息" : context.state.headline)
                        .font(.caption)
                        .lineLimit(2)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    if context.state.bestReply.isEmpty {
                        Text("还没有候选回复")
                            .font(.caption2).foregroundStyle(.secondary)
                    } else {
                        HStack(spacing: 6) {
                            Text("#1 \(context.state.bestReply)")
                                .font(.caption)
                                .lineLimit(2)
                            Spacer(minLength: 0)
                            Text(context.state.bestScore > 0
                                 ? "\(Int(context.state.bestScore * 100))%"
                                 : "")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            } compactLeading: {
                // 紧凑态只放危险等级：一眼看出要不要立刻管
                DangerBadge(danger: context.state.danger, compact: true)
            } compactTrailing: {
                Text(context.state.hasCandidates ? "待选" : "Jev")
                    .font(.caption2)
            } minimal: {
                DangerBadge(danger: context.state.danger, compact: true)
            }
        }
    }
}

private struct LockScreenView: View {
    let state: JevActivityAttributes.ContentState

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            DangerBadge(danger: state.danger)
            VStack(alignment: .leading, spacing: 4) {
                Text(state.chatTitle)
                    .font(.caption2).foregroundStyle(.secondary)
                Text(state.headline.isEmpty ? "等待对方消息" : state.headline)
                    .font(.subheadline).bold()
                if !state.bestReply.isEmpty {
                    Text("#1 \(state.bestReply)")
                        .font(.footnote)
                        .lineLimit(3)
                }
                if state.hasCandidates {
                    Text("候选已就绪 —— 在通知上点选，再回微信长按粘贴")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
    }
}

/// 危险等级徽标。0~9 分三档配色，和安卓悬浮窗的语义保持一致。
private struct DangerBadge: View {
    let danger: Double
    var compact: Bool = false

    private var color: Color {
        switch danger {
        case ..<3: return .green
        case ..<6: return .orange
        default: return .red
        }
    }

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: compact ? 8 : 10, height: compact ? 8 : 10)
            if !compact {
                Text(String(format: "危险 %.0f/9", danger))
                    .font(.caption).bold()
                    .foregroundStyle(color)
            }
        }
    }
}
