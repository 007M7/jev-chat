import Foundation
import UserNotifications
import UIKit

/// 结果输出的第一个通道：**通知横幅 + 3 个候选按钮**（用户已确认采用）。
///
/// 为什么用通知：iOS 上没有跨 App 悬浮窗，通知横幅是唯一能盖在微信上方、
/// 又能承载交互（点按钮）的系统通道。用户点某个候选 → 该候选进剪贴板 →
/// 回微信长按粘贴。**程序从不自动发送**，这条红线在 iOS 上天然更安全：
/// App 根本碰不到微信的输入框。
final class NotificationPresenter: NSObject {

    static let categoryId = "jev_candidates"
    private static let actionPrefix = "jev_cand_"

    /// 当前待选的候选，供点击回调取用（通知里只能带 action identifier，
    /// 候选原文太长不适合塞进 payload，所以留在内存里）
    private var pending: [String] = []

    func setUp() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, error in
            if let error {
                NSLog("[Jev] 通知授权失败: \(error.localizedDescription)")
            }
        }
        // 3 个候选各做一个按钮。UNNotificationAction 最多给 4 个，正好够。
        let actions = (0..<3).map { i in
            UNNotificationAction(
                identifier: "\(Self.actionPrefix)\(i)",
                title: "候选 \(i + 1)",
                options: []          // 不设 .foreground：点完留在微信里，不跳出 App
            )
        }
        let category = UNNotificationCategory(
            identifier: Self.categoryId,
            actions: actions,
            intentIdentifiers: [],
            options: []
        )
        center.setNotificationCategories([category])
    }

    /// 弹一条带候选的通知。
    /// - Parameters:
    ///   - candidates: 已按 Jev 排序的候选（最多 3 条，顺序即 #1/#2/#3）
    ///   - headline: 一句话结论，放在横幅正文里
    ///   - chatTitle: 会话名
    func present(candidates: [String], headline: String, chatTitle: String) {
        guard !candidates.isEmpty else { return }
        pending = Array(candidates.prefix(3))

        let content = UNMutableNotificationContent()
        content.title = "Jev · \(chatTitle)"
        // 正文把候选原文列出来，这样即使不点按钮也能先看到内容
        var lines = [headline]
        for (i, c) in pending.enumerated() {
            lines.append("#\(i + 1) \(c)")
        }
        content.body = lines.joined(separator: "\n")
        content.categoryIdentifier = Self.categoryId
        content.interruptionLevel = .active     // 别被专注模式静默掉

        let req = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil                            // nil = 立即送达
        )
        UNUserNotificationCenter.current().add(req) { error in
            if let error {
                NSLog("[Jev] 通知发送失败: \(error.localizedDescription)")
            }
        }
    }

    private func copyToClipboard(_ text: String) {
        UIPasteboard.general.string = text
    }
}

extension NotificationPresenter: UNUserNotificationCenterDelegate {

    /// 点按钮：把选中的候选写进剪贴板，然后用户自己回微信长按粘贴。
    /// 注意这里**没有任何发送动作**——App 没有往别的 App 输入框写文本的能力，
    /// 这也是项目"绝不自动发送"红线在 iOS 上的天然保障。
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                               didReceive response: UNNotificationResponse,
                               withCompletionHandler completionHandler: @escaping () -> Void) {
        let id = response.actionIdentifier
        if id.hasPrefix(Self.actionPrefix),
           let idx = Int(id.dropFirst(Self.actionPrefix.count)),
           idx < pending.count {
            copyToClipboard(pending[idx])
            NSLog("[Jev] 已把候选 #\(idx + 1) 写入剪贴板，等待用户手动粘贴")
        }
        completionHandler()
    }

    /// 前台也显示横幅：App 在前台时用户更可能正在看分析结果，不该被静默
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                               willPresent notification: UNNotification,
                               withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
