import Foundation
import UIKit

/// 本地变化检测门（Tier 0）。**这是整个方案的成本闸门。**
///
/// 为什么必须在本地做：云端感知（DeepSeek 视觉）一次约 3~5 秒且要花钱，
/// 绝不能每帧都调。实测采集是 30~60fps，即每秒可能上百帧。
///
/// 设计承自桌面实现的实测经验（那套已经调过阈值并验证过）：
///   1. **像素门**：只看消息区，且只有真的变了才往下走。
///      不看整屏——输入框光标闪烁会让整屏每帧都在变。
///   2. **静默门**：消息区连续 quiet 秒没变才算"稳定"，避免把滚动中的中间态送出去。
///   3. **上限兜底**：带 maxWait 上限，避免会动的表情包让静默永远等不到。
///
/// **关键：决策必须由定时器驱动，不能只在帧到达时判定。**
/// 这是桌面版踩过的坑，原文：「WGC 只在内容变化时送帧，所以'最后一帧到达'不等于
/// '已经静默'——静默判定必须由独立定时器做，否则内容稳定后不再送帧、稳定帧永远发不出去。」
/// 所以这里分成两个入口：
///   - `ingest(image:at:)`：帧到达时调，只更新"画面有没有变"
///   - `tick(at:)`：定时器调（0.5s），负责静默/上限/去重的判定与放行
final class FrameGate {

    struct Decision {
        let shouldAnalyze: Bool
        /// 给界面解释"为什么这次没送" / "为什么送了"
        let reason: String
    }

    struct Config {
        /// 消息区：纵向比例区间（避开顶部状态栏/导航与底部输入框）
        var bandTop: Double = 0.15
        var bandBottom: Double = 0.80
        /// 缩略图尺寸：越小越省，48x96 足够判断"画面变没变"
        var thumbWidth: Int = 48
        var thumbHeight: Int = 96
        /// 单个像素的灰度差超过这个值算"这个像素变了"
        var pixelDelta: Int = 12
        /// 变化像素占比超过这个值算"画面变了"
        var changedRatio: Double = 0.02
        /// 连续多少秒没变才算稳定
        var quietSeconds: Double = 1.5
        /// 最多等多久就必须下决定（应对会动的表情包）
        var maxWaitSeconds: Double = 6.0
    }

    private let config: Config
    private var lastThumb: [UInt8]?
    /// 上一次"稳定且已送去分析"的缩略图，用来避免同一屏反复送
    private var lastAnalyzedThumb: [UInt8]?
    private var lastChangeAt: Date?
    private var waitingSince: Date?

    init(config: Config = Config()) {
        self.config = config
    }

    /// 喂一帧，**只更新"画面有没有变"**，不做放行判定。
    /// 放行交给 tick(at:)，因为画面稳定后可能不再来帧（见类型注释里的坑）。
    func ingest(image: UIImage, at now: Date) {
        guard let thumb = Self.thumbnail(of: image, config: config) else { return }
        if let prev = lastThumb {
            let ratio = Self.changedRatio(prev, thumb, pixelDelta: config.pixelDelta)
            if ratio >= config.changedRatio { lastChangeAt = now }
        } else {
            lastChangeAt = now
        }
        lastThumb = thumb
        if waitingSince == nil { waitingSince = now }
    }

    /// 由定时器调（0.5s 一次），负责静默门 / 上限兜底 / 去重，并给出放行判定。
    func tick(at now: Date) -> Decision {
        guard let thumb = lastThumb else {
            return Decision(shouldAnalyze: false, reason: "还没收到帧")
        }
        if waitingSince == nil { waitingSince = now }

        let quietFor = now.timeIntervalSince(lastChangeAt ?? now)
        let waited = now.timeIntervalSince(waitingSince ?? now)

        // 静默够久 → 稳定了，可以送
        if quietFor >= config.quietSeconds {
            // 内容与"上次已分析的那屏"相同就不重复送
            if let last = lastAnalyzedThumb,
               Self.changedRatio(last, thumb, pixelDelta: config.pixelDelta) < config.changedRatio {
                waitingSince = nil
                return Decision(shouldAnalyze: false, reason: "稳定但与上次分析时相同，跳过")
            }
            lastAnalyzedThumb = thumb
            waitingSince = nil
            return Decision(shouldAnalyze: true, reason: String(format: "画面已稳定 %.1fs", quietFor))
        }

        // 静默等不到（比如表情包在动）→ 到上限也送一次
        if waited >= config.maxWaitSeconds {
            waitingSince = nil
            lastAnalyzedThumb = thumb
            return Decision(shouldAnalyze: true, reason: String(format: "等待 %.0fs 仍未静默，按上限送出", waited))
        }

        return Decision(shouldAnalyze: false,
                        reason: String(format: "等待静默 %.1fs/%.1fs", quietFor, config.quietSeconds))
    }

    func reset() {
        lastThumb = nil
        lastAnalyzedThumb = nil
        lastChangeAt = nil
        waitingSince = nil
    }

    // MARK: 缩略图与比对

    /// 把消息区那条带子降采样成灰度小图，用来比对。返回 nil 表示图像不可用。
    static func thumbnail(of image: UIImage, config: Config) -> [UInt8]? {
        guard let cg = image.cgImage else { return nil }
        let w = cg.width, h = cg.height
        let y0 = Int(Double(h) * config.bandTop)
        let y1 = Int(Double(h) * config.bandBottom)
        guard y1 > y0, let crop = cg.cropping(to: CGRect(x: 0, y: y0, width: w, height: y1 - y0)) else {
            return nil
        }

        let tw = config.thumbWidth, th = config.thumbHeight
        var buf = [UInt8](repeating: 0, count: tw * th)
        let cs = CGColorSpaceCreateDeviceGray()
        guard let ctx = CGContext(data: &buf, width: tw, height: th,
                                  bitsPerComponent: 8, bytesPerRow: tw,
                                  space: cs,
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
        ctx.interpolationQuality = .low       // 比对用，不需要好画质
        ctx.draw(crop, in: CGRect(x: 0, y: 0, width: tw, height: th))
        return buf
    }

    /// 有多少比例的像素灰度差超过阈值
    static func changedRatio(_ a: [UInt8], _ b: [UInt8], pixelDelta: Int) -> Double {
        let n = min(a.count, b.count)
        guard n > 0 else { return 0 }
        var diff = 0
        for i in 0..<n {
            if abs(Int(a[i]) - Int(b[i])) > pixelDelta { diff += 1 }
        }
        return Double(diff) / Double(n)
    }
}
