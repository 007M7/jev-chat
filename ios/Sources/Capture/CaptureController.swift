import Foundation
import ScreenCaptureKit
import UIKit
import CoreImage
import os

/// 采集控制器 —— 同时是 Gate 1 的探针。
///
/// 它要回答三个未知点（在写任何业务代码前必须知道答案，任一失败全自动路线就要改设计）：
///   1. 采集授权是否每次启动/每次开会话都要重新确认？
///   2. 屏幕顶部/灵动岛是否有录屏指示条常驻？
///   3. 免费签名（Sideloadly/AltStore 自签）能不能带 `screen-capture` 后台模式，
///      也就是切到微信后帧还来不来？—— 这是全自动的命门。
///
/// 前两点只能靠人看屏幕回答，所以界面上要显示状态；第三点靠这里的后台帧计数回答。
/// 计数会落盘，切后台期间的数据不会丢。
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
    /// 变化检测门的当前判定，显示在界面上（"为什么这次没送/送了"）
    @Published private(set) var gateStatus: String = "未开始"
    /// 门放行的次数：正常工作时它应该远小于总帧数——这就是成本控制的效果
    @Published private(set) var stableFrameCount = 0

    /// **扩展点**：门判定"画面已稳定"时回调，接感知层。
    /// 下一轮把 PerceptionClient 挂到这里即可，采集与限流不用动。
    var onStableFrame: ((UIImage) -> Void)?

    private let gate = FrameGate()

    /// 采样到的帧（内存里只留最近一帧，用于"看一下它到底截到了什么"）
    private(set) var lastFrame: UIImage?

    private var stream: SCStream?
    private var filter: SCContentFilter?
    private var tickTimer: Timer?

    /// 后台区间记录：进后台时的帧数 → 回前台时的帧数。差值非 0 就说明后台还在收帧。
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

    // MARK: 生命周期

    override init() {
        super.init()
        loadLog()

        // 前后台切换：这是判断"后台还收不收帧"的依据
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

    // MARK: 开始采集

    /// 由用户点按钮触发。ScreenCaptureKit 要求通过系统选择器拿授权，
    /// 不能自己静默开始 —— 这一步就是 iOS 的"同意采集"。
    ///
    /// 注意：macOS 上可以用 `SCContentSharingPickerConfiguration.allowedPickerModes`
    /// 限定只选整屏，**iOS 上没有这个属性**（首次云构建实测报 'unavailable in iOS'）。
    /// 所以这里不设配置，交给系统选择器，用户自己选「整个屏幕」。
    /// 选到别的来源也不致命：探针会如实在界面上显示帧尺寸与后台收帧情况。
    func requestPermissionAndStart() {
        lastError = nil
        let picker = SCContentSharingPicker.shared
        picker.add(self)
        statusText = "等待你在系统选择器里选「整个屏幕」…"
        picker.present()
    }

    func stop() {
        Task {
            try? await stream?.stopCapture()
            stream = nil
            isCapturing = false
            statusText = "已停止"
            tickTimer?.invalidate()
            tickTimer = nil
        }
    }

    private func startStream(with filter: SCContentFilter) async {
        self.filter = filter
        let config = SCStreamConfiguration()
        // **iOS 上不能设帧率**：SCStreamConfiguration.minimumFrameInterval 在 iOS 上
        // 报 'unavailable in iOS'（首次云构建实测）。showsCursor 同样不可用。
        // 所以限流只能自己做：见 stream(_:didOutputSampleBuffer:of:) 里的时间门。
        // 这反而更合理——聊天界面变化很慢，按时间抽帧比按帧率设限更直接。

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: Self.frameQueue)
            try await s.startCapture()
            self.stream = s
            self.isCapturing = true
            self.statusText = "采集中"
            self.startTicking()
        } catch {
            self.lastError = "启动采集失败: \(error.localizedDescription)"
            self.statusText = "启动失败"
        }
    }

    /// 每 0.5 秒做两件事：刷新"最近一帧是几秒前"，并驱动变化检测门。
    ///
    /// **门必须由定时器驱动**：画面稳定后可能不再来帧（桌面版实测踩过这个坑），
    /// 只在帧到达时判定的话，稳定帧永远发不出去。
    private func startTicking() {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshLastFrameAgo()
                self?.tickGate()
            }
        }
    }

    private func tickGate() {
        let decision = gate.tick(at: Date())
        gateStatus = decision.reason
        guard decision.shouldAnalyze, let img = lastFrame else { return }
        stableFrameCount += 1
        // 交给上层（下一轮 = 感知 → 判断 → 通知/灵动岛）
        onStableFrame?(img)
    }

    private var lastFrameAt: Date?
    private func refreshLastFrameAgo() {
        guard let t = lastFrameAt else { lastFrameAgo = "—"; return }
        let dt = Date().timeIntervalSince(t)
        lastFrameAgo = String(format: "%.1f 秒前", dt)
    }

    // MARK: 前后台记账

    private func handleEnterBackground() {
        isAppInBackground = true
        sessions.append(BackgroundSession(enteredAt: Date(), exitedAt: nil,
                                         framesAtEnter: totalFrames, framesAtExit: nil))
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
        saveLog()
    }

    // MARK: 把最近一帧存相册，用于人工核对"到底截到了什么"

    func saveLastFrameToPhotos() {
        guard let img = lastFrame else { lastError = "还没有收到帧"; return }
        UIImageWriteToSavedPhotosAlbum(img, nil, nil, nil)
    }

    private static let frameQueue = DispatchQueue(label: "jev.capture.frames")
    /// CIContext 只在采集队列上用，避免多线程竞争
    private nonisolated static let ciContext = CIContext(options: [.useSoftwareRenderer: false])
    /// 只在采集队列（串行）上读写，所以不需要额外加锁
    private nonisolated(unsafe) static var frameCounter = 0
    /// 上一次把帧转成图片的时间。iOS 上不能设帧率，限流靠它。
    private nonisolated(unsafe) static var lastSnapshotAt: Date?

    /// 在采集队列上把帧变成简单值 + 偶发的一张图片，再回主线程更新状态。
    /// 刻意不把 CMSampleBuffer 跨 actor 传递——它不是 Sendable，越过 actor 边界
    /// 在严格并发下会报错，而且那张 buffer 的生命周期由系统管。
    private func noteFrame(width: Int, height: Int, snapshot: UIImage?, at time: Date) {
        totalFrames += 1
        lastFrameAt = time
        frameSize = "\(width)×\(height)"
        if let snapshot {
            lastFrame = snapshot
            // 喂给变化检测门：只更新"画面有没有变"，放行判定在定时器里做
            gate.ingest(image: snapshot, at: time)
        }
        if totalFrames % 100 == 0 { saveLog() }
    }
}

// MARK: - SCStreamOutput 收帧

extension CaptureController: SCStreamOutput {
    nonisolated func stream(_ stream: SCStream,
                           didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                           of type: SCStreamOutputType) {
        guard type == .screen,
              let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let w = CVPixelBufferGetWidth(pb)
        let h = CVPixelBufferGetHeight(pb)
        let now = Date()

        // 限流必须自己做：iOS 上没有 minimumFrameInterval（云构建实测不可用）。
        // 每帧只数数（极便宜），**最多每 500ms 转一张图**（转图是最贵的操作）。
        // 这就是"Tier 0 变化检测"的起点——后续在这里加像素差判断，
        // 只有消息区真的变了才把图送到感知层。
        Self.frameCounter += 1
        var snapshot: UIImage?
        let shouldSnap = Self.lastSnapshotAt.map { now.timeIntervalSince($0) >= 0.5 } ?? true
        if shouldSnap {
            Self.lastSnapshotAt = now
            let ci = CIImage(cvPixelBuffer: pb)
            if let cg = Self.ciContext.createCGImage(ci, from: ci.extent) {
                snapshot = UIImage(cgImage: cg)
            }
        }

        Task { @MainActor in
            self.noteFrame(width: w, height: h, snapshot: snapshot, at: now)
        }
    }
}

// MARK: - SCStreamDelegate 出错/被系统停掉

extension CaptureController: SCStreamDelegate {
    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        Task { @MainActor in
            // 用户从控制中心停掉录屏、或权限被收回时会走这里
            self.isCapturing = false
            self.statusText = "流被停止"
            self.lastError = "采集流被停止: \(error.localizedDescription)"
        }
    }
}

// MARK: - 系统采集选择器回调
//
// 注意：这几个回调的签名以 iOS 27 SDK 为准。首次云构建如果报签名不符，
// 按编译错误提示调整参数名即可（SCContentSharingPickerObserver 是新协议，
// 我这边的文档来源只能确认方法名大意，不能保证逐字一致）。

extension CaptureController: SCContentSharingPickerObserver {
    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker,
                                         didUpdateWith filter: SCContentFilter,
                                         for stream: SCStream?) {
        Task { @MainActor in
            self.statusText = "已授权，正在启动采集…"
            await self.startStream(with: filter)
        }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker,
                                         didCancelFor stream: SCStream?) {
        Task { @MainActor in
            self.statusText = "你取消了授权"
        }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        Task { @MainActor in
            self.lastError = "选择器失败: \(error.localizedDescription)"
            self.statusText = "授权失败"
        }
    }
}
