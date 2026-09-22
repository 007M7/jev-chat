# iOS 原生工程

这一步的目标是回答 **Gate 1 的三个未知点**，不是做业务。任一失败，"全自动读屏"就要改设计。

## 为什么显示层是「通知 + 灵动岛」而不是悬浮窗

你在 Windows 桌面版做的那个**半透明置顶悬浮窗，在 iOS 上做不到**——iOS 不提供跨 App 绘制的能力
（Android 是 `TYPE_APPLICATION_OVERLAY`，桌面版是 tkinter 置顶窗，iOS 没有对应物）。
这是系统限制，不是实现难度。iOS 上能盖在微信上的系统通道只有四个：

| 通道 | 能盖在微信上 | 容量 | 交互 | 本工程 |
|---|---|---|---|---|
| 通知横幅 + 按钮 | ✅ | 3 条候选 + 一行判断 | ✅ 点按钮选候选 → 进剪贴板 | **已实现** |
| 灵动岛 / 实时活动 | ✅ 常驻不遮挡 | 危险等级 + 最佳候选 1 条 | ❌ 只能看 | **已实现** |
| 画中画窗口 | ✅ | 完整面板 | 有限 | 未做（App Store 灰色地带） |
| 自定义键盘面板 | ✅ 占键盘位 | 完整面板 | ✅ | 未做（看不到对方消息，价值低） |

两个一起用最接近悬浮窗体验：**灵动岛常驻显示危险等级和最佳候选，通知横幅负责让你点选**。

## 怎么用

### 1. 推到 GitHub

工程里有 `.github/workflows/ios-unsigned.yml`。你需要把仓库推到 GitHub（**这一步必须你来做**，按 `CLAUDE.md` 第 7 条我不执行 `git commit`/`push`）：

```bash
git add ios .github tools docs
git commit -m "feat(ios): Gate 1 采集探针 + 通知/灵动岛输出"
git push
```

### 2. 手动跑一次 Action

GitHub 仓库页 → **Actions** → 左侧选「iOS 未签名 IPA」→ **Run workflow**。
`note` 可以填 `probe`。

跑完在 **Artifacts** 里下载 `JevAssistant-unsigned-<note>`，里面是 `JevAssistant-unsigned.ipa`。

> 第一步的日志会打印这台 runner 上有哪些 Xcode。**如果报 "没有 iOS 27+ SDK"，后面都会失败**——
> 那是硬前提（ScreenCaptureKit 的 iOS 版从 27.0 起才有），需要换 runner 或自建。
> 流水线里还有一步会验证 `UIBackgroundModes: screen-capture` 真的进了产物，没进就直接失败，
> 免得装到手机上才白测一轮。

### 3. 装到手机

Windows 上用 **Sideloadly** 或 **AltStore**：

1. 手机 U 盘连电脑
2. Sideloadly 里选 `JevAssistant-unsigned.ipa`，填你的 Apple ID（建议用 App 专用密码）
3. 装完在手机 **设置 → 通用 → VPN与设备管理** 里信任证书
4. 免费账号签出来的 **7 天有效**，到期重签一次（AltStore 可以在同一 WiFi 下自动刷新）

### 4. 跑 Gate 1 核对

打开 App，点「开始采集」→ 系统弹出内容选择器 → 选**整个屏幕**。然后回答界面上列的问题：

| # | 要回答的 | 怎么测 | 影响 |
|---|---|---|---|
| ① | 授权是否每次启动/每次都重新问？ | 点两次「开始采集」，看第二次是否还要重选 | 若要每次重选 → 退化成半自动 |
| ② | 有没有录屏指示条常驻？ | 采集时看屏幕顶部/灵动岛 | 体验问题，你能否接受 |
| ③ | 切到微信后还收帧吗？ | 开始采集 → 切到微信停 30 秒并滑动 → 回来读那行 | **命门**。收不到 → 全自动走不通 |

③ 由代码自动判断并显示结论（录了进/出后台的帧数差，还会落盘，所以切后台期间的数据不丢）。
①② 只能靠你看屏幕——界面上就是那两个选择题，选完截图给我。

顺手点一下「弹一条带 3 个候选的通知」，验一下输出通道：点候选按钮后该候选会进剪贴板
（**不发送**），回微信长按即可粘贴。

### 5. 另外顺手记录两件事

- **帧尺寸**（界面显示，例如 `1179×2556`）：决定一次感知调用的图片体积与成本
- **切到微信 30 秒内大约收多少帧**：用来定抽帧率（现在是 2fps）

## 工程结构

```
ios/
  project.yml                  # XcodeGen 工程定义（不提交 .xcodeproj）
  Shared/                      # App 与 widget 扩展共同编译
    JevActivityAttributes.swift
  Sources/                     # App target
    App/JevApp.swift           # 入口 + 输出层自测
    App/ProbeView.swift        # Gate 1 核对界面
    Capture/CaptureController.swift   # SCContentSharingPicker + SCStream + 后台帧记账
    Output/NotificationPresenter.swift # 通知横幅 + 3 个候选按钮
    Output/LiveActivityController.swift # 灵动岛/实时活动
  Widget/                      # widget 扩展 target
    JevWidgetBundle.swift      # 灵动岛与锁屏的渲染
```

## 数据闭环：记录导出到 PC

App 的 Info.plist 已打开文件共享（`UIFileSharingEnabled` + `LSSupportsOpeningDocumentsInPlace`），
所以记录能从手机拷出来，在 PC 上做三件事：

**导出**：手机「文件」App → 我的 iPhone → Jev 助手 → 拷出 `jev_*.json`
（`jev_history.json` 分析记录 / `jev_profiles.json` 会话档案 / `jev_persons.json` 人物档案）

**PC 侧处理**：

```bash
cd tools/jev
python import_app_records.py --export <导出目录> --review out/app_review.md      # 可读复盘
python import_app_records.py --export <导出目录> --label-table out/group_labels.md  # 群聊校准核对表
python import_app_records.py --export <导出目录> --distill-json out/for_distill.json # 记忆蒸馏输入
python import_app_records.py --export <导出目录> --stats                          # 只看统计
```

**为什么需要这条链**：群聊题目集标着 `calibrated: false`，修它只能靠真实标注数据；
而记忆蒸馏需要对话原文作为出处（记忆必须带 evidence）。两者都要把记录拿出手机。
记录里**只存文本不存截图**，与"截图不落盘"的既有约束一致。

## 还没接的部分（下一轮）

这一版**只验证采集与显示**，没有把感知和判断接进来。下一轮要做：

1. **抽帧变化检测**（Tier 0）：现在每帧只记数。桌面版 `desktop/capture.py` 已经把
   像素门 + 静默门验证过了，直接照搬思路即可——只在消息区像素变了才往下走。
2. **感知调用**：把帧转 JPEG → 调 DeepSeek 视觉 → 结构化对话。provider 配置复用
   `tools/jev/providers.json`（Swift 侧读同一个 JSON，别再写一份）。
3. **判断 + 起草 + 排序**：`JevClient` 的 Swift 版，题目集从 `tools/jev/questions.json` 读，
   按 `is_group` 选档案。
4. **会话隔离与标题容错**：桌面版 `desktop/engine.py` 已经踩过这两个坑
   （视觉模型会把标题读错，实测同一个群名被读出过两种写法，例如「技术交流群(50)」被读成「技术交楼群(50)」；
   不隔离会话会让 state 混两个会话的内容）。iOS 侧同样需要，照搬。

## 已知风险（诚实清单）

| 风险 | 状态 |
|---|---|
| ~~CI runner 没有 Xcode 27~~ | ✅ **已解决**：`macos-15` 上只有 Xcode 26.3 / iOS 26.2 SDK，改用 `xcode-27` 镜像后有 **Xcode 27.0 / iOS 27.0 SDK**，编译通过 |
| ~~Swift API 签名需要微调~~ | ✅ **已解决**：云构建报出 3 个错误，全是 macOS 有、iOS 没有的属性（见下） |
| ~~widget 扩展嵌入~~ | ✅ **已解决**：`JevWidget.appex` 已正确嵌入 `JevAssistant.app/PlugIns/` |
| 免费签名可能不支持 `screen-capture` 后台模式 | ⏳ 这正是 Gate 1 ③ 要回答的，只能真机验 |
| 真机授权是否每次都要重新确认 | ⏳ Gate 1 ①，只能真机验 |

### 云构建查出的真实能力差异：iOS 的 ScreenCaptureKit 比 macOS 窄

首次云构建报出三个属性 `'unavailable in iOS'`：

| 属性 | 影响 | 应对 |
|---|---|---|
| `SCStreamConfiguration.minimumFrameInterval` | **不能设帧率** | 限流改为自己做：每帧只计数，最多每 500ms 转一张图（见 `FrameGate`） |
| `SCStreamConfiguration.showsCursor` | 不能隐藏光标 | 无影响 |
| `SCContentSharingPickerConfiguration.allowedPickerModes` | 不能限定"只选整屏" | 交给系统选择器，用户自己选「整个屏幕」 |

这说明**抽帧与变化检测在 iOS 上是必需品而不是优化项**——没有 API 帮你限速。

## 别人怎么用（分发）

**现在没法直接把 IPA 发给别人装**——iOS 要求每个 App 用开发者证书签名，而证书只对签名者
自己的设备有效。可行的路径与各自的代价见 [docs/ios/distribution.md](../docs/ios/distribution.md)：

- **零成本**：每个用户自己 fork → 用自己的 Actions 构建 → 用自己的 Apple ID 自签（7 天有效期）
- **想让普通人也能装**：买 $99/年开发者账号走 TestFlight（无需电脑、无需信任、90 天一个构建）
- **上架**：技术可行，但审核对"聊天辅助"这类工具有不确定性，需先解决定位表述

无论哪条路，用户都要**自备 Jev 与 DeepSeek 的 API 密钥**，并在 App 里填（只存本机 Keychain）。

## 与桌面版/安卓版的关系

- **共享**：题目档案（`tools/jev/questions.json`）、provider 配置（`providers.json`）、
  视觉 prompt（`tools/ios-spike/vlm_extract.py` 的 `SYSTEM_PROMPT`）、一致性规则（`consistency.py`）
- **不共享**：采集层与显示层——这三端各自被平台限制约束，没有通用做法
- 桌面版 `desktop/` 已经在调用 `tools/ios-spike/pipeline.py` 的
  `pick_profile` / `to_python_state` / `judge` / `rank`，这些是**跨端共享契约，改动要同步**
