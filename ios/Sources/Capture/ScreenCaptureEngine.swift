import Foundation
import ScreenCaptureKit
import UIKit

/// iOS 27 专用的采集引擎。
///
/// **为什么要单独一个类**：ScreenCaptureKit 的 iOS 版从 27.0 才有，
/// 而 ScreenCaptureKit 的类型出现在存储属性上时，整个类都会被绑到 27.0 ——
/// 那样 App 就无法安装到 iOS 26 的设备上（实测用户手机是 26.6.2）。
/// 把带 SCStream 的代码全部关在这里、并标 `@available(iOS 27.0, *)`，
/// 上层控制器就能在 iOS 26 上正常编译与运行（只是没有采集能力）。
///
/// 对外只通过闭包回传数据，所以调用方不需要 import ScreenCaptureKit。
///
/// **关于"不要每次开始采集都弹确认"**（用户实测反馈）：查过 iOS 27 SDK 的
/// ScreenCaptureKit 头文件与 .swiftinterface，结论是硬限制 + 一条可做的优化：
///   - SDK 里没有任何持久化 API（`persist` / `NSSecureCoding` / `archive` 零命中），
///     `SCShareableContent` / `SCDisplay` / `SCWindow` 全是 `API_UNAVAILABLE(ios)`，
///     所以 iOS 上**只能**从 picker 拿到 `SCContentFilter`，也无法把它存盘复用。
///     跨 App 重启必然要再确认一次，这是系统隐私保证，绕不过去。
///   - 但**同一次运行内不必重复确认**：filter 与 stream 都可以留着，
///     stop 之后重新 startCapture() 即可。本类就是按这个思路改的
///     （见 `pause()` / `resume()`），只有首次或用户主动"重新选目标"时才弹 picker。
@available(iOS 27.0, *)
final class ScreenCaptureEngine: NSObject {

    // MARK: 回调（都在主线程）

    /// 收到一帧。width/height 是原生像素尺寸；snapshot 是限流后的抽样图（可能为 nil）
    var onFrame: ((Int, Int, UIImage?, Date) -> Void)?
    /// 状态文案变化
    var onStatus: ((String) -> Void)?
    /// 出错
    var onError: ((String) -> Void)?

    private var stream: SCStream?
    private var filter: SCContentFilter?
    /// 是否已经拿到过授权（有 filter）。上层据此决定"直接恢复"还是"弹 picker"。
    var isAuthorized: Bool { filter != nil }

    private static let frameQueue = DispatchQueue(label: "jev.capture.frames")
    private nonisolated(unsafe) static var frameCounter = 0
    private nonisolated(unsafe) static var lastSnapshotAt: Date?
    private nonisolated static let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    // MARK: 开始/停止

    /// 通过系统选择器取授权。这一步就是 iOS 的"同意采集"，不能静默开始。
    ///
    /// **必须先把 picker 激活（`isActive = true`），否则 `present()` 静默什么都不做** ——
    /// 实测现象是"点了开始采集，什么面板都不弹"。Apple 的 iOS 示例在 present() 之前
    /// 有一个 `activatePicker()` 步骤，作用就是设置这个属性：
    ///   `picker.defaultConfiguration = ...; activatePicker(); picker.present()`
    /// 我第一版漏了它，症状与用户反馈完全一致。
    ///
    /// `presentPicker(usingContentStyle: .display)` 比 `present()` 少一步：
    /// 直接进到"选屏幕"这一步，不再让用户先选"要共享窗口还是屏幕"。
    /// iPhone 上能共享的本来就只有整屏，所以这一步是纯多余。`.display` 在
    /// iOS 27 是可用的（`SCShareableContentStyleDisplay` 标了 ios(27.0)）。
    func requestPermission() {
        let picker = SCContentSharingPicker.shared
        picker.add(self)                 // 注册观察者，这样才能收到用户选定的 filter

        // 不显示麦克风控制：我们要的是画面，收音频只会多要一个权限
        var config = SCContentSharingPickerConfiguration()
        config.showsMicrophoneControl = false
        picker.defaultConfiguration = config

        // 先问系统"这台设备上屏幕录制到底可不可用"，避免失败时只有一个含糊的错误。
        // （`isAvailable` 是 iOS 27 新增，SDK 头文件标了 ios(27.0)。）
        guard picker.isAvailable else {
            onError?("系统不允许屏幕录制：到「设置 → 隐私与安全性 → 屏幕录制」里允许本 App，然后重试。")
            onStatus?("未获屏幕录制权限")
            return
        }

        picker.isActive = true           // ← 关键，漏了它 present() 无效
        onStatus?("等待你在系统选择器里确认共享屏幕…")
        picker.presentPicker(usingContentStyle: .display)
    }

    /// 首次：拿到 filter 后建流并开播。
    func start(with filter: SCContentFilter) async {
        self.filter = filter
        await startStream(with: filter)
    }

    /// 再次开始：复用已有的 filter 与 stream，**不弹 picker**。
    /// 失败（权限被收回、流已被系统销毁等）时返回 false，交给上层回退到弹 picker。
    @discardableResult
    func resume() async -> Bool {
        guard let f = filter else { return false }
        if let s = stream {
            do {
                try await s.startCapture()
                onStatus?("采集中（已复用上次的屏幕授权，未再次确认）")
                return true
            } catch {
                // 流已不可用：丢掉它，用现有 filter 重建一个
                stream = nil
                onError?("恢复采集流失败，正在用已有授权重建：\(error.localizedDescription)")
            }
        }
        guard await startStream(with: f) else { return false }
        onStatus?("采集中（复用上次的屏幕授权，未再次确认）")
        return true
    }

    /// 临时停止：**保留 filter 与 stream**，下次 resume() 才能不弹 picker。
    func pause() {
        Task {
            try? await stream?.stopCapture()
        }
        onStatus?("已停止（授权保留，再点开始不用重新确认）")
    }

    /// 彻底停止并丢掉授权：下次开始会重新弹 picker（换目标/排查问题用）。
    func stop() {
        Task {
            try? await stream?.stopCapture()
            stream = nil
        }
        filter = nil
    }

    private func startStream(with filter: SCContentFilter) async -> Bool {
        let config = SCStreamConfiguration()
        // iOS 上不能设 minimumFrameInterval / showsCursor（macOS 专属，云构建实测报
        // 'unavailable in iOS'），所以帧率限流只能自己做——见 didOutputSampleBuffer。
        let s = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: Self.frameQueue)
            try await s.startCapture()
            self.stream = s
            onStatus?("采集中")
            return true
        } catch {
            onError?("启动采集失败: \(error.localizedDescription)")
            onStatus?("启动失败")
            return false
        }
    }
}

// MARK: - 收帧

@available(iOS 27.0, *)
extension ScreenCaptureEngine: SCStreamOutput {
    nonisolated func stream(_ stream: SCStream,
                           didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                           of type: SCStreamOutputType) {
        guard type == .screen,
              let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let w = CVPixelBufferGetWidth(pb)
        let h = CVPixelBufferGetHeight(pb)
        let now = Date()

        // 每帧只计数（很便宜），最多每 500ms 转一张图（转图是最贵的操作）。
        // 这就是"Tier 0 变化检测"的起点：喂给 FrameGate 的正是这个抽样图。
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

        let frame = (w, h, snapshot, now)
        DispatchQueue.main.async { [weak self] in
            self?.onFrame?(frame.0, frame.1, frame.2, frame.3)
        }
    }
}

// MARK: - 流被系统停掉（用户从控制中心停止录屏、权限被收回等）

@available(iOS 27.0, *)
extension ScreenCaptureEngine: SCStreamDelegate {
    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        let msg = error.localizedDescription
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            // 流被系统销毁了（用户从控制中心停录、权限被收回、目标窗口没了……）：
            // 丢掉 stream，让下一次 resume() 用现有 filter 重建；filter 先留着——
            // 权限往往还在，重建能直接成功，没必要让用户再确认一次。
            self.stream = nil
            self.onStatus?("流被停止")
            self.onError?("采集流被停止: \(msg)")
        }
    }
}

// MARK: - 系统选择器回调

@available(iOS 27.0, *)
extension ScreenCaptureEngine: SCContentSharingPickerObserver {
    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker,
                                         didUpdateWith filter: SCContentFilter,
                                         for stream: SCStream?) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.onStatus?("已授权，正在启动采集…")
            Task { await self.start(with: filter) }
        }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker,
                                         didCancelFor stream: SCStream?) {
        DispatchQueue.main.async { [weak self] in
            self?.onStatus?("你取消了授权")
        }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        let msg = error.localizedDescription
        DispatchQueue.main.async { [weak self] in
            self?.onError?("选择器失败: \(msg)")
            self?.onStatus?("授权失败")
        }
    }
}
