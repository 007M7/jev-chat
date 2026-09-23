import Foundation
import UserNotifications
import UIKit

/// 结果输出的第一个通道：**通知横幅 + 3 个候选按钮**。
///
/// 为什么用通知：iOS 上没有跨 App 悬浮窗，通知横幅是唯一能盖在微信上方、
/// 又能承载交互（点按钮）的系统通道。用户点某个候选 → 该候选进剪贴板 →
/// 回微信长按粘贴。**程序从不自动发送**，这条红线在 iOS 上天然更安全：
/// App 根本碰不到微信的输入框。
///
/// ## 候选为什么必须写进通知的 userInfo
///
/// 第一版把候选只存在内存里（一个 `pending` 数组），点击时从内存取。**实测剪贴板是空的**：
/// iOS 在微信前台时可能已把 App 进程挂起甚至回收，点按钮时系统是**重新拉起 App 来投递动作**的，
/// 此时内存里那个数组是空的，于是"复制"复制了个空字符串。
/// 现在候选同时写进 `content.userInfo`，点击时从**通知自身**读——与进程状态无关。
///
/// ## 代理为什么在 AppDelegate 里注册
///
/// 后台拉起时 SwiftUI 的 `.onAppear` 不会执行，所以 `UNUserNotificationCenter.delegate`
/// 必须在 `didFinishLaunchingWithOptions` 里就设好，否则动作回调根本进不来。
final class NotificationPresenter: NSObject {

    /// 全局单例：代理要在 AppDelegate 里注册，而 AppBridge 也要用它，两者必须是同一个对象
    static let shared = NotificationPresenter()

    static let categoryId = "jev_candidates"
    private static let actionPrefix = "jev_cand_"
    private static let userInfoKey = "candidates"
    private static let analysisIdKey = "analysis_id"

    /// 由 AppDelegate 在启动时调用：注册代理与动作类别
    func register() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self

        let actions = (0..<3).map { i in
            UNNotificationAction(identifier: "\(Self.actionPrefix)\(i)",
                                 title: "候选 \(i + 1)",
                                 options: [])   // 不设 .foreground：点完留在微信里
        }
        center.setNotificationCategories([
            UNNotificationCategory(identifier: Self.categoryId,
                                   actions: actions,
                                   intentIdentifiers: [],
                                   options: [])
        ])

        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error { NSLog("[Jev] 通知授权失败: \(error.localizedDescription)") }
            NSLog("[Jev] 通知授权 granted=\(granted)")
        }
    }

    /// 当前的通知授权状态。
    ///
    /// 为什么要把它显示出来：用户反馈过"关掉安静模式也不弹通知"。
    /// 系统层的授权一旦被拒（比如重装后 Bundle ID 变了、重新弹窗时点了不允许），
    /// `UNUserNotificationCenter.add` 会**静默失败**——代码这边看不出任何异常，
    /// 用户那边什么都收不到。所以这个状态必须可见，而且要能一键跳到系统设置。
    static func authorizationStatusText(_ completion: @escaping (String, Bool) -> Void) {
        UNUserNotificationCenter.current().getNotificationSettings { s in
            let (text, ok): (String, Bool)
            switch s.authorizationStatus {
            case .authorized: text = "已允许"; ok = true
            case .provisional: text = "临时允许（安静投递）"; ok = true
            case .notDetermined: text = "还没问过（点下面的测试通知会弹窗）"; ok = false
            case .denied: text = "**被系统拒绝了** —— 去系统设置里打开"; ok = false
            case .ephemeral: text = "临时授权"; ok = true
            @unknown default: text = "未知状态"; ok = false
            }
            DispatchQueue.main.async { completion(text, ok) }
        }
    }

    /// 快速模式（只出判断、没有候选）时的通知：只报结论，不带候选按钮。
    func presentVerdict(headline: String, chatTitle: String) {
        let content = UNMutableNotificationContent()
        content.title = "Jev · \(chatTitle)"
        content.body = headline
        content.interruptionLevel = .active
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        ) { error in
            if let error { NSLog("[Jev] 结论通知发送失败: \(error.localizedDescription)") }
        }
    }

    /// 后台动作里没法弹 UI，用一条静默通知给用户反馈——否则他无法确认"复制成功了没有"
    private func confirmCopied(_ text: String) {
        let c = UNMutableNotificationContent()
        c.title = "已复制到剪贴板"
        c.body = text + "\n\n回微信长按输入框 → 粘贴"
        c.interruptionLevel = .active
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)
        )
    }

    /// 弹一条带候选的通知。
    /// - Parameters:
    ///   - candidates: 已按 Jev 排序的候选（最多 3 条，顺序即 #1/#2/#3）
    ///   - headline: 一句话结论，放在横幅正文里
    ///   - chatTitle: 会话名
    ///   - analysisID: 这条通知对应的分析记录 id，用于把"用户选了哪条"记回记录里
    func present(candidates: [String], headline: String, chatTitle: String, analysisID: String = "") {
        guard !candidates.isEmpty else { return }
        let top = Array(candidates.prefix(3))

        let content = UNMutableNotificationContent()
        content.title = "Jev · \(chatTitle)"
        var lines = [headline]
        for (i, c) in top.enumerated() { lines.append("#\(i + 1) \(c)") }
        content.body = lines.joined(separator: "\n")
        content.categoryIdentifier = Self.categoryId
        content.interruptionLevel = .active
        // **关键**：候选取自通知自身，这样无论进程是否还在内存里都能取到正确文本
        var info: [String: Any] = [Self.userInfoKey: top]
        if !analysisID.isEmpty { info[Self.analysisIdKey] = analysisID }
        content.userInfo = info

        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        ) { error in
            if let error { NSLog("[Jev] 通知发送失败: \(error.localizedDescription)") }
        }
    }
}

extension NotificationPresenter: UNUserNotificationCenterDelegate {

    /// 点候选按钮：把选中的候选写进剪贴板，用户自己回微信长按粘贴。
    /// 这里**没有任何发送动作**——App 没有往别的 App 输入框写文本的能力，
    /// 这也是项目"绝不自动发送"红线在 iOS 上的天然保障。
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                               didReceive response: UNNotificationResponse,
                               withCompletionHandler completionHandler: @escaping () -> Void) {
        let id = response.actionIdentifier
        let info = response.notification.request.content.userInfo
        let candidates = (info[Self.userInfoKey] as? [String]) ?? []

        guard id.hasPrefix(Self.actionPrefix),
              let idx = Int(id.dropFirst(Self.actionPrefix.count)),
              idx < candidates.count else {
            NSLog("[Jev] 通知动作未处理: action=\(id) 候选数=\(candidates.count)")
            completionHandler()
            return
        }

        let text = candidates[idx]
        UIPasteboard.general.string = text
        NSLog("[Jev] 已把候选 #\(idx + 1) 写入剪贴板（\(text.count) 字）")

        // 把"用户选了哪条"记回记录里——这是"建议好不好"的唯一客观真值，
        // 将来做校准（Calibration）就靠它。进程可能被重建过，但 store 会从磁盘读回。
        if let aid = info[Self.analysisIdKey] as? String, !aid.isEmpty {
            AnalysisStore.shared.markPicked(analysisID: aid, index: idx)
        }

        confirmCopied(text)
        completionHandler()
    }

    /// 前台也显示横幅：App 在前台时用户更可能正在看分析结果，不该被静默
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                               willPresent notification: UNNotification,
                               withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}

/// 只为在启动时注册通知代理而存在。
/// SwiftUI 的 `.onAppear` 在"被后台拉起投递通知动作"时不会执行，
/// 所以代理必须在这里设好。
final class JevAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        NotificationPresenter.shared.register()
        return true
    }
}
