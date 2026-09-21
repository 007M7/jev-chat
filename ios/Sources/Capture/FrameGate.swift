import Foundation
import UIKit

/// 本地变化检测门（Tier 0）。**这是整个方案的成本闸门。**
///
/// 为什么必须在本地做：云端感知（DeepSeek 视觉）一次约 3~5 秒且要花钱，
/// 绝不能每帧都调。实测采集是 30~60fps，即每秒可能上百帧。
///
/// 设计沿用桌面版 `desktop/capture.py` 已验证的那套（那边写在文档字符串里）：
///   1. **像素门**：只看消息区，且只有真的变了才往下走。
///      不看整屏——输入框光标闪烁会让整屏每帧都在变。
///   2. **静默门**：消息区连续 quiet 秒没变才算"稳定"，避免把滚动中的中间态送出去。
///   3. **上限兜底**：带 maxWait 上限，避免会动的表情包让静默永远等不到。
///   4. **去重**：交给上层用消息签名做（引擎里已有），这里只管"画面变了没"。
///
/// iOS 与桌面版的差异：桌面上能单独截微信窗口，iOS 只能拿到整屏，
/// 所以"消息区"只能用一条比例带近似（默认纵向 15%~80%），并且要主动避开
/// 底部输入框所在的区域。这条带子是启发式，写在这里而不是散在调用处。
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
    private var lastDecisionAt: Date?

    init(config: Config = Config()) {
        self.config = config
    }

    /// 喂一帧，返回是否需要送去分析。每帧都要调（很便宜：只是缩略图比对）。
    func ingest(image: UIImage, at now: Date) -> Decision {
        guard let thumb = Self.thumbnail(of: image, config: config) else {
            return Decision(shouldAnalyze: false, reason: "缩略图失败")
        }

        // 与上一帧比：变了就刷新"最后变化时间"
        let changed: Bool
        if let prev = lastThumb {
            let ratio = Self.changedRatio(prev, thumb, pixelDelta: config.pixelDelta)
            changed = ratio >= config.changedRatio
            if changed { lastChangeAt = now }
        } else {
            changed = true
            lastChangeAt = now
        }
        lastThumb = thumb

        if waitingSince == nil { waitingSince = now }

        let quietFor = now.timeIntervalSince(lastChangeAt ?? now)
        let waited = now.timeIntervalSince(waitingSince ?? now)

        // 静默够久 → 稳定了，可以送
        if quietFor >= config.quietSeconds {
            // 但内容与"上次已分析的那屏"相同就不重复送
            if let last = lastAnalyzedThumb, Self.changedRatio(last, thumb, pixelDelta: config.pixelDelta) < config.changedRatio {
                waitingSince = nil
                return Decision(shouldAnalyze: false, reason: "画面稳定但与上次分析时相同，跳过")
            }
            lastAnalyzedThumb = thumb
            waitingSince = nil
            lastDecisionAt = now
            return Decision(shouldAnalyze: true, reason: String(format: "画面已稳定 %.1fs", quietFor))
        }

        // 静默等不到（比如表情包在动）→ 到上限也送一次
        if waited >= config.maxWaitSeconds {
            waitingSince = nil
            lastAnalyzedThumb = thumb
            lastDecisionAt = now
            return Decision(shouldAnalyze: true, reason: String(format: "等待 %.0fs 仍未静默，按上限送出", waited))
        }

        return Decision(shouldAnalyze: false,
                        reason: changed ? "画面在变" : String(format: "等待静默 %.1fs/%.1fs", quietFor, config.quietSeconds))
    }

    func reset() {
        lastThumb = nil
        lastAnalyzedThumb = nil
        lastChangeAt = nil
        waitingSince = nil
        lastDecisionAt = nil
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
