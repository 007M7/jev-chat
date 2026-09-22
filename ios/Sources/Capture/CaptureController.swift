import Foundation
import UIKit

/// 采集控制器 —— 界面状态 + 变化检测 + 后台记账。
///
/// **这里刻意不 import ScreenCaptureKit。** 真正的采集在 `ScreenCaptureEngine` 里，
/// 那个类标了 `@available(iOS 27.0, *)`。原因是 ScreenCaptureKit 的 iOS 版从 27.0 才有，
/// 而用户手机实测是 iOS 26.6.2：如果采集类型出现在本类的存储属性上，整个 App 就装不上
/// iOS 26 的设备。拆开之后：
///   - iOS 26：App 能装能跑，采集按钮提示"需要 iOS 27"，但通知/灵动岛照常可验
///   - iOS 27：采集能力自动启用，不需要换包
///
/// 它同时是 Gate 1 的探针：要回答"切到微信后还收不收帧"这个命门问题，
/// 所以进/出后台的帧数差会落盘，切后台期间的数据不会丢。
@MainActor
final class CaptureController: NSObject, ObservableObject {

    // MARK: 对外状态（界面绑定）

    @Published private(set) var isCapturing = false
    @Published private(set) var statusText = "未开始"
    @Published private(set) var totalFrames = 0
    @Published private(set) var lastFrameAgo: String = "—"
    @Published private(set) var frameSize: String = "—"
    @Published private(set) var isAppInBackground = false
    @Published private(set) var lastError: String?
    /// 变化检测门的当前判定（"为什么这次没送/送了"）
    @Published private(set) var gateStatus: String = "未开始"
    /// 门放行的次数：正常工作时它应远小于总帧数——这就是成本控制的效果
    @Published private(set) var stableFrameCount = 0
    /// 当前系统是否支持采集（iOS 27+）。界面上要如实显示。
    @Published private(set) var captureSupported = true

    /// **扩展点**：门判定"画面已稳定"时回调，接感知层。
    var onStableFrame: ((UIImage) -> Void)?

    private let gate = FrameGate()
    private var lastFrameAt: Date?
    /// iOS 27 上是 ScreenCaptureEngine；其它系统上为 nil
    private var engine: AnyObject?
    private var tickTimer: Timer?
    /// 采样到的最近一帧（只在内存里，供变化检测与"存相册核对"）
    private(set) var lastFrame: UIImage?

    // MARK: 后台区间记账

    struct BackgroundSession: Codable {
        var enteredAt: Date
        var exitedAt: Date?
        var framesAtEnter: Int
        var framesAtExit: Int?
        var framesWhileBackgrounded: Int? { framesAtExit.map { $0 - framesAtEnter } }
    }

    private(set) var sessions: [BackgroundSession] = []
    private let logURL: URL = {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return dir.appendingPathComponent("capture_probe.json")
    }()

    override init() {
        super.init()
        loadLog()
        if #available(iOS 27.0, *) {
            captureSupported = true
        } else {
            captureSupported = false
            statusText = "此系统不支持采集（需 iOS 27）"
        }

        let nc = NotificationCenter.default
        nc.addObserver(forName: UIApplication.didEnterBackgroundNotification,
                       object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.handleEnterBackground() }
        }
        nc.addObserver(forName: UIApplication.willEnterForegroundNotification,
                       object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.handleEnterForeground() }
        }
    }

    // MARK: 开始 / 停止

    func requestPermissionAndStart() {
        lastError = nil
        guard #available(iOS 27.0, *) else {
            lastError = "系统采集需要 iOS 27（ScreenCaptureKit 的 iOS 版从 27.0 才有）。"
                + "当前系统是 iOS \(UIDevice.current.systemVersion)，请先升级系统。"
            statusText = "不支持采集"
            return
        }
        gate.reset()
        startTicking()

        // **已经拿到过授权就直接恢复，不再弹系统的屏幕共享确认。**
        // 用户实测反馈"每次开始采集都弹确认键"——同一次运行内这是可以避免的：
        // filter 与 stream 都留着，stop 之后重新 startCapture() 即可。
        // （跨 App 重启必然要再确认一次：iOS 27 的 ScreenCaptureKit 没有任何
        //  filter 持久化 API，SCShareableContent 在 iOS 上也不可用，见引擎里的说明。）
        if #available(iOS 27.0, *), let e = engine as? ScreenCaptureEngine, e.isAuthorized {
            resumeExisting(e)
            return
        }
        makeEngineAndAskPermission()
    }

    /// 复用已有授权继续采集（失败则回退到弹 picker）。
    ///
    /// 单独一个方法而不是写在 `requestPermissionAndStart` 里，是因为它的参数与
    /// 局部变量都是 iOS 27 才有的类型：**在带可用性标注的方法里，闭包内部也在
    /// "已确认可用"的上下文里**；而把 Task 闭包直接写在 `if #available` 分支里时，
    /// 闭包是否继承外层的可用性收敛不够确定，本地又没编译器可验。宁可写长一点。
    @available(iOS 27.0, *)
    private func resumeExisting(_ e: ScreenCaptureEngine) {
        statusText = "正在恢复采集（不用再确认）…"
        isCapturing = true
        Task { @MainActor in
            let ok = await e.resume()
            if !ok {
                // 复用失败（权限被收回等）→ 老老实实回到弹 picker 的路径
                self.makeEngineAndAskPermission()
            }
        }
    }

    /// 造一个新引擎并走系统选择器。首次开始、或用户主动"重新选采集目标"时走这里。
    private func makeEngineAndAskPermission() {
        guard #available(iOS 27.0, *) else { return }
        let e = ScreenCaptureEngine()
        wire(e)
        engine = e
        isCapturing = true
        e.requestPermission()
    }

    /// 重新选采集目标：丢掉已有授权，下次开始会重新弹 picker。
    func reselectTarget() {
        lastError = nil
        if #available(iOS 27.0, *) {
            (engine as? ScreenCaptureEngine)?.stop()
            engine = nil
        }
        isCapturing = false
        statusText = "已清除屏幕授权，下次开始会重新让你选"
    }

    /// 把引擎的三个回调接到本控制器。
    /// 标 `@available(iOS 27.0, *)` 是因为参数类型本身只在 iOS 27 存在——
    /// 签名里出现受限类型就必须自己带上可用性标注，否则编译不过。
    @available(iOS 27.0, *)
    private func wire(_ e: ScreenCaptureEngine) {
        // 引擎回调在主线程发出，这里再显式切一次主 actor，保证状态更新安全
        e.onFrame = { [weak self] w, h, img, at in
            Task { @MainActor in
                self?.noteFrame(width: w, height: h, snapshot: img, at: at)
            }
        }
        e.onStatus = { [weak self] s in
            Task { @MainActor in
                guard let self else { return }
                self.statusText = s
                // 状态推进到"采集中"说明这次真的起来了——把上一条失败文案清掉。
                // 实测踩过：旧错误与新状态并存（"启动采集失败: 无法开始流播放" + "采集中"），
                // 让人以为现在还是坏的。
                if s.hasPrefix("采集中") {
                    self.lastError = nil
                    self.isCapturing = true
                }
            }
        }
        e.onError = { [weak self] m in
            Task { @MainActor in
                guard let self else { return }
                self.lastError = m
                self.isCapturing = false
                // 失败时不能让状态栏继续显示"采集中"（否则界面自相矛盾）
                self.statusText = "启动失败"
            }
        }
    }

    /// 停止采集：**保留屏幕授权**，所以下次开始不会再弹确认。
    func stop() {
        if #available(iOS 27.0, *) {
            (engine as? ScreenCaptureEngine)?.pause()
        }
        isCapturing = false
        tickTimer?.invalidate()
        tickTimer = nil
    }

    // MARK: 定时驱动

    /// 每 0.5 秒：刷新"最近一帧是几秒前"，并驱动变化检测门。
    ///
    /// **门必须由定时器驱动**：ScreenCaptureKit 与 WGC 都只在内容变化时送帧，
    /// 所以"最后一帧到达"不等于"已经静默"——靠帧到达判定的话，内容稳定后不再送帧，
    /// 稳定帧永远发不出去。（这条是桌面版采集层实测踩过的坑，iOS 上同样成立。）
    private func startTicking() {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshLastFrameAgo()
                self?.tickGate()
            }
        }
    }

    private func refreshLastFrameAgo() {
        guard let t = lastFrameAt else { lastFrameAgo = "—"; return }
        lastFrameAgo = String(format: "%.1f 秒前", Date().timeIntervalSince(t))
    }

    private func tickGate() {
        let decision = gate.tick(at: Date())
        gateStatus = decision.reason
        guard decision.shouldAnalyze, let img = lastFrame else { return }
        stableFrameCount += 1
        // 交给上层（下一轮 = 感知 → 判断 → 通知/灵动岛）
        onStableFrame?(img)
    }

    private func noteFrame(width: Int, height: Int, snapshot: UIImage?, at time: Date) {
        totalFrames += 1
        lastFrameAt = time
        frameSize = "\(width)×\(height)"
        if let snapshot {
            lastFrame = snapshot
            gate.ingest(image: snapshot, at: time)   // 只更新"画面变没变"
        }
        if totalFrames % 100 == 0 { saveLog() }
    }

    // MARK: 前后台记账

    private func handleEnterBackground() {
        isAppInBackground = true
        sessions.append(BackgroundSession(enteredAt: Date(), exitedAt: nil,
                                         framesAtEnter: totalFrames, framesAtExit: nil))
        // 这份日志是 Gate 1 探针的取证材料（"切到微信后还收不收帧"），
        // 已经答完了，但留着当诊断有用。加个上限免得无限增长——它每次前后台切换都追加一条。
        if sessions.count > 200 { sessions.removeFirst(sessions.count - 200) }
        saveLog()
    }

    private func handleEnterForeground() {
        isAppInBackground = false
        if var last = sessions.popLast(), last.exitedAt == nil {
            last.exitedAt = Date()
            last.framesAtExit = totalFrames
            sessions.append(last)
        }
        saveLog()
    }

    /// **Gate 1 的核心结论**：切到微信的那些区间里，帧有没有继续来。
    func backgroundFrameSummary() -> String {
        let done = sessions.filter { $0.framesAtExit != nil }
        guard !done.isEmpty else { return "还没切过后台，切到微信待一会儿再回来看" }
        let total = done.compactMap { $0.framesWhileBackgrounded }.reduce(0, +)
        let detail = done.suffix(3).map { s -> String in
            let secs = String(format: "%.0f", (s.exitedAt ?? Date()).timeIntervalSince(s.enteredAt))
            return "\(secs)s 内 \(s.framesWhileBackgrounded ?? 0) 帧"
        }.joined(separator: "；")
        return total > 0
            ? "✅ 后台继续收帧：共 \(total) 帧（\(detail)）→ screen-capture 后台模式生效"
            : "❌ 后台收不到帧（\(detail)）→ 全自动路线走不通，需要改设计"
    }

    // MARK: 落盘

    private func loadLog() {
        guard let data = try? Data(contentsOf: logURL),
              let decoded = try? JSONDecoder().decode([BackgroundSession].self, from: data) else { return }
        sessions = decoded
    }

    private func saveLog() {
        guard let data = try? JSONEncoder().encode(sessions) else { return }
        try? data.write(to: logURL, options: .atomic)
    }

    func resetLog() {
        sessions.removeAll()
        totalFrames = 0
        stableFrameCount = 0
        saveLog()
    }

    // MARK: 存最近一帧到相册（人工核对"到底截到了什么"）

    func saveLastFrameToPhotos() {
        guard let img = lastFrame else { lastError = "还没有收到帧"; return }
        UIImageWriteToSavedPhotosAlbum(img, nil, nil, nil)
    }
}
