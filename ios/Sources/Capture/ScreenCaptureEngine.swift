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
    private static let frameQueue = DispatchQueue(label: "jev.capture.frames")
    private nonisolated(unsafe) static var frameCounter = 0
    private nonisolated(unsafe) static var lastSnapshotAt: Date?
    private nonisolated static let ciContext = CIContext(options: [.useSoftwareRenderer: false])

    // MARK: 开始/停止

    /// 通过系统选择器取授权。这一步就是 iOS 的"同意采集"，不能静默开始。
    func requestPermission() {
        let picker = SCContentSharingPicker.shared
        picker.add(self)
        onStatus?("等待你在系统选择器里选「整个屏幕」…")
        picker.present()
    }

    func start(with filter: SCContentFilter) async {
        self.filter = filter
        let config = SCStreamConfiguration()
        // iOS 上不能设 minimumFrameInterval / showsCursor（macOS 专属，云构建实测报
        // 'unavailable in iOS'），所以帧率限流只能自己做——见 didOutputSampleBuffer。
        let s = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: Self.frameQueue)
            try await s.startCapture()
            self.stream = s
            onStatus?("采集中")
        } catch {
            onError?("启动采集失败: \(error.localizedDescription)")
            onStatus?("启动失败")
        }
    }

    func stop() {
        Task {
            try? await stream?.stopCapture()
            stream = nil
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
            self?.onStatus?("流被停止")
            self?.onError?("采集流被停止: \(msg)")
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
