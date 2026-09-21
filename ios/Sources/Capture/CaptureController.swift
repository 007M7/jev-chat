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
    func requestPermissionAndStart() {
        lastError = nil
        let picker = SCContentSharingPicker.shared
        var config = SCContentSharingPickerConfiguration()
        // 只允许整屏采集：我们要读的是微信，不是本 App 自己的窗口
        config.allowedPickerModes = .singleDisplay
        picker.defaultConfiguration = config
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
        // 抽帧率压到 2fps：聊天界面变化很慢，2fps 足够。
        config.minimumFrameInterval = CMTime(value: 1, timescale: 2)
        config.showsCursor = false
        // 刻意**不设** width/height：探针阶段先用原生分辨率，把 API 猜测面降到最小
        // （缩放属于后续为云端调用省钱的优化，等确认能跑起来再动）。
        // 实际帧尺寸会显示在界面上，用来估算一次分析的图片体积。

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

    /// 每 0.5 秒刷新一次"最近一帧是几秒前"，用来肉眼判断流有没有断
    private func startTicking() {
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshLastFrameAgo() }
        }
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

    /// 在采集队列上把帧变成简单值 + 偶发的一张图片，再回主线程更新状态。
    /// 刻意不把 CMSampleBuffer 跨 actor 传递——它不是 Sendable，越过 actor 边界
    /// 在严格并发下会报错，而且那张 buffer 的生命周期由系统管。
    private func noteFrame(width: Int, height: Int, snapshot: UIImage?, at time: Date) {
        totalFrames += 1
        lastFrameAt = time
        frameSize = "\(width)×\(height)"
        if let snapshot { lastFrame = snapshot }
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

        // 每 20 帧抽一帧转图片（在采集队列上做，不把 buffer 跨线程传），
        // 供"存相册核对到底截到了什么"。其余帧只记数，不转图——转图是这里最贵的操作。
        Self.frameCounter += 1
        var snapshot: UIImage?
        if Self.frameCounter % 20 == 1 {
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
