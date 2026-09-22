# Jev 助手 · iOS 版

**iPhone 上的对话副驾。** 它在你聊天时读屏、读懂对方、给出候选回复，你点一下候选就进剪贴板，粘贴发送全由你自己来。

和 Android 版共用同一套「大脑」（七道判断题 + 起草 + 排序），但**采集与显示层是为 iOS 重写的**——iOS 上没有无障碍节点树、没有跨 App 悬浮窗、也不能往别的 App 里注入文字，这两层不能照搬。

| | |
|---|---|
| 系统要求 | **iOS 27 及以上**（采集依赖 iOS 版的 ScreenCaptureKit）；iOS 26 能装能跑，但没有采集能力 |
| 构建 | GitHub Actions 出**未签名 IPA**，Windows 上用 Sideloadly / AltStore 自签安装 |
| 安装成本 | 免费 Apple ID 签出来的包 **7 天有效**，到期重签 |
| 上架 | 无。不依赖 App Store，也不需要付费开发者账号 |
| 许可 | MIT（与主项目一致） |

> 逐项实现进度、验证结论与实测数据见 **[docs/ios/implementation_status.md](../docs/ios/implementation_status.md)**。
> 本文件讲"是什么、怎么用、边界在哪"。

---

## 一、iOS 上的三个硬限制（决定了这个版本长什么样）

这三条不是实现难度问题，是系统不提供能力。设计上的取舍都源于它们：

| 限制 | 后果 |
|---|---|
| **没有跨 App 悬浮窗** | 显示层只能用「通知横幅 + 锁屏实时活动」，做不到 Android 那种常驻半透明面板 |
| **不能向别的 App 注入文字** | **没有"一键填入"**。iOS 沙箱不允许，最接近的做法是"点候选 → 进剪贴板 → 你长按粘贴" |
| **没有跨 App 的 UI 树可读** | 读对话只能靠**屏幕像素**。Android 版读无障碍节点拿气泡 bounds 的做法在这里不可用 |

另外三条实测确认过的平台细节（都查过 SDK，不是推测）：

- **只有从系统选择器拿到的授权才能采集屏幕**。iOS 上 `SCShareableContent` / `SCDisplay` / `SCWindow` 全是 `API_UNAVAILABLE(ios)`，ScreenCaptureKit 也没有任何保存授权（filter）的接口，所以**每次重开 App 都要确认一次**共享屏幕；同一次运行内的开始/停止不再重复确认。
- **ScreenCaptureKit 的 iOS 版比 macOS 窄**：`SCStreamConfiguration.minimumFrameInterval`、`showsCursor`、`SCContentSharingPickerConfiguration.allowedPickerModes` 在 iOS 上不可用（编译期可确认），所以帧率限流只能自己做。
- **采集必须在后台也活着**。靠 `UIBackgroundModes: screen-capture`，已真机验证切到微信后仍在收帧——这是"全自动"成立的命门。

---

## 二、它怎么工作

```
屏幕帧（约 30–60 fps）
   │
   ├─ 每 500ms 采样一张图 ──→ 变化检测门（本地，免费）
   │                            · 像素门：只比消息区那条带子，变了才算
   │                            · 静默门：连续 1.5s 没变 = 画面稳定，才继续
   │                            · 上限门：最多等 6s 就放行（应付会动的表情包）
   │                            · 去重：与"上次已分析的那屏"相同就跳过
   │
   ├─ 视觉模型把整屏读成结构化对话（谁说的 / 说了什么 / 表情包与图片转成文字描述）
   │
   ├─ 并进该会话累积的对话流（跨帧按内容去重），取最近 10 条构造 state
   │
   ├─ Jev 七道题（意图 / 是否在跟我说话 / 风险等级 / 该不该回 …）
   │
   ├─ 起草 3 条候选 + 排序（可关：快速模式只出判断）
   │
   └─ 通知横幅（3 个按钮，点了进剪贴板） + 锁屏实时活动
```

**实测耗时**（同一批真实截图，多次）：

| 环节 | 耗时 | 备注 |
|---|---|---|
| 感知（读屏） | **4.4 ~ 15.5 s** | 波动最大，是瓶颈；思考型模型把时间花在推理上 |
| 判断（七道题） | 1.0 ~ 1.7 s | 很快 |
| 起草（3 条候选） | 2.2 ~ 7.0 s | 快速模式可跳过 |
| 排序 | 0.9 ~ 1.1 s | 快速模式可跳过 |
| **合计** | **8.7 ~ 16.5 s** | |

所以它**不是实时副驾**，是"看到消息、想一句怎么回"这段时间里的辅助。要更快，杠杆在感知模型：设置页可以把它换成非思考型视觉模型。

---

## 三、红线（与主项目一致）

- **只读屏幕**。不 hook、不注入、不改微信/QQ 等任何 App、不读它们的数据库、不抓它们的流量。
- **绝不自动发送**。不点任何 App 的发送按钮、不碰转账/红包/收款相关界面。填入输入框这类动作在 iOS 上本来也做不到，但设计上同样不做。
- **截图不落盘**。画面只在内存里保留最近一帧，用完即弃；落盘的只有文字（会话记录 + 分析记录）。诊断页有一个"存最近一帧到相册"按钮，那是唯一会把图写到本机的操作，且由你手动触发。
- **密钥只进 Keychain**。不写进任何配置文件、不进日志、不随导出记录离开手机。配置文件里只写"密钥的账号名"。

---

## 四、功能现状

完整清单见 [docs/ios/implementation_status.md](../docs/ios/implementation_status.md)，这里只给概览：

| 能力 | 状态 |
|---|---|
| 全屏采集 + 后台继续采集 | ✅ 真机验证 |
| 变化检测门（成本闸门） | ✅ 已实现（阈值在 `FrameGate.Config`） |
| 视觉感知（整屏 → 结构化对话） | ✅ 已实现 |
| 七道题判断 + 起草 + 排序 | ✅ 已实现，与 Android 版同一套题目 |
| 群聊区分发言人 | ✅ 每条消息带发言人；群聊/单聊走不同题目档案 |
| 跨帧上下文累积 | ✅ 按会话累积对话流，判断用最近 10 条 |
| 记录（分析记录 + 会话记录） | ✅ 按会话分群聊/单聊；可查 |
| 联系人目录 | ✅ 可检索、可编辑身份，身份跨会话通用 |
| Provider 与模型自由配置 | ✅ 三个角色各自选服务与模型；可自加 provider |
| 通知候选 + 剪贴板 | ✅ 真机验证 |
| 自动填入选中的候选 | ❌ iOS 不允许 |
| 锁屏/灵动岛实时活动 | ⚠️ 代码已写，但 widget 扩展当前未嵌入（免签账号的 Bundle ID 约束），**尚未生效** |
| 群聊题目集校准 | ⚠️ 需要真实标注数据（导出记录 → PC 标注）；目前只有一对一那套校准过 |

---

## 五、构建

不需要 Mac：工程用 [XcodeGen](https://github.com/yonaskolb/XcodeGen) 定义（`project.yml`），`.xcodeproj` 不入库，云构建上现生成。

**云构建（不需要 Mac 的路）**

1. Fork 本仓库
2. Actions → 「iOS 未签名 IPA」→ Run workflow
3. 跑完在 Artifacts 里下载 `JevAssistant-unsigned-<note>`，解压得到 `JevAssistant-unsigned.ipa`

流水线里有几处刻意的硬失败——宁可构建失败，也不要产出一个装不上或跑不了的包：没有 iOS 27+ SDK 直接失败；构建后校验 `screen-capture` 后台模式真的进了产物；校验三份共享配置能解析。

**本地构建（有 Mac 时）**

```bash
brew install xcodegen
mkdir -p ios/Resources && cp tools/jev/questions.json tools/jev/prompts.json tools/jev/providers.json ios/Resources/
cd ios && xcodegen generate
xcodebuild -project JevAssistant.xcodeproj -scheme JevAssistant -sdk iphoneos -configuration Release
```

`Resources/` 里的三份 JSON 是**构建时**从 `tools/jev/` 拷进去的，仓库里不放副本——它们是 Python 原型、桌面版与 iOS 三端共用的单一来源，抄一份就一定会漂移。

仓库里还有个 `.github/workflows/sdk-probe.yml`，用来回答"某个 API 在 iOS 27 SDK 里到底有没有、签名是什么"：它把 SDK 的 `.swiftinterface` 与头文件打出来，还能用 `swiftc -typecheck` 直接验证一段调用代码。它确实解决过实际问题——`presentPickerUsingContentStyle:` 导入 Swift 后叫什么名字是**试出来的**（答案是 `present(using:)`），不是猜的。

---

## 六、安装（Windows，无需 Mac）

1. 装 Apple 驱动与 [Sideloadly](https://sideloadly.io/)（iTunes 的 web 版带驱动；注意 iTunes 的 `/quiet` 会**静默跳过** Apple 驱动，只看退出码会误判）
2. 手机 USB 连电脑，Sideloadly 里选 `JevAssistant-unsigned.ipa`，填你的 Apple ID（手机号注册的账号要写国际区号格式，如 `+86…`）
3. 手机上：**设置 → 通用 → VPN 与设备管理 → 开发者 App** → 信任
4. 免费账号的签名 **7 天**有效，到期重签一次；AltStore 可以在同一 WiFi 下自动刷新

Sideloadly 的「Remove app extensions」要勾上——免费签名下扩展的 Bundle ID 会和主 App 对不上，导致包里结构不合法（表现为「不受信任的开发者」且信任条目生不出来）。代价是实时活动不可用，主功能不受影响。

---

## 七、配置：步骤与模型

三步各自独立选服务，**不用改代码、不用重新构建**：

| 步骤 | 做什么 | 默认 |
|---|---|---|
| 判断 | 七道题 + 候选排序 | TypeSafe 直连的 Jev |
| 起草 | 生成 3 条候选回复 | DeepSeek |
| 感知 | 读屏（**必须是视觉模型**） | DeepSeek |

在设置 →「Provider 与模型」里可以：改 Base URL、选 API 格式（Chat Completions / TypeSafe SystemOne）、填密钥（存 Keychain）、维护模型列表（增删改、启停、标注上下文长度与「视觉」能力）、自己加一家 provider。

改动只写"与默认不同的部分"，存在 App 私有目录的 `jev_providers_user.json`（**不含密钥**），删掉它就恢复出厂默认。这样以后仓库更新默认配置时，你没改过的部分照样生效。

PC 侧同一套配置，也可以不改仓库地覆盖：把 `JEV_PROVIDERS_OVERLAY` 指向一份库外 JSON，然后 `python tools/jev/providers.py --show` 看每个角色最终解析到哪个服务、哪个模型。

**一个提醒**：感知必须用有视觉能力的模型。选了没有视觉能力的模型，感知会一直失败。

---

## 八、数据闭环

App 沙盒的 Documents 在「文件」App 里可见（`UIFileSharingEnabled`），记录可以拷出来：

| 文件 | 内容 |
|---|---|
| `jev_history.json` | 分析记录：每次判断的结论、候选、耗时、原始答案 |
| `jev_conversations.json` | 会话记录：跨帧累积的对话流，逐条带发言人 |
| `jev_profiles.json` / `jev_persons.json` | 会话档案 / 人物档案 |
| `jev_providers_user.json` | 你的 provider 覆盖层（无密钥） |

拿到 PC 上就能做**群聊题目集校准**（目前只有一对一那套校准过）和**记忆蒸馏**。这是"提高准确度"最实在的一条路：每条分析天然就是一条标注样本。

---

## 九、已知限制（诚实清单）

- **不是实时**。一轮 8.7~16.5 秒，瓶颈在感知。要快就换非思考型视觉模型。
- **每次重开 App 要确认一次屏幕共享**。这是 iOS 的隐私保证，SDK 里没有任何持久化接口可以绕过。
- **屏幕顶部会常驻录屏指示条**（灵动岛位置）。系统行为，去不掉。
- **感知有间歇性空返回**：思考型模型偶发把额度烧在推理上、正文为空（约 8 次里 1 次），下一轮自愈，但那次分析丢失。
- **群聊题目集未校准**。群聊与单聊走不同档案，群聊那份还标着 `calibrated: false`，结论只能参考；一对一那份是校准过的。
- **只在 iOS 27.0 + 深色模式 + 默认主题下真机验证过**。浅色模式、自定义聊天背景、其他聊天 App 的布局都没验。
- **实时活动未生效**（widget 扩展当前未嵌入）。已生效的显示通道是通知横幅。
- **需要 iOS 27**。iOS 26 能装，但没有采集能力。

---

## 十、目录结构

```
ios/
├── project.yml                 # XcodeGen 工程定义（.xcodeproj 不入库）
├── Sources/
│   ├── App/                    # 界面：探针、记录、联系人、设置、provider 管理
│   ├── Capture/                # 采集：ScreenCaptureKit 引擎 + 变化检测门
│   ├── Brain/                  # 大脑：配置、感知/判断/起草流水线、本地记录
│   └── Output/                 # 输出：通知横幅、剪贴板、实时活动
├── Shared/                     # App 与 widget 扩展共用的类型
├── Widget/                     # 实时活动扩展（当前未嵌入，见 project.yml 注释）
└── Resources/                  # 构建时从 tools/jev/ 拷入，不入库
```

`Sources/Capture/CaptureController.swift` 刻意**不 import ScreenCaptureKit**：那些类型只在 iOS 27 存在，一旦出现在属性类型上，整个类都会被绑到 27.0，App 就装不到 iOS 26 的设备上。带 ScreenCaptureKit 的代码全部关在 `ScreenCaptureEngine` 里并标 `@available(iOS 27.0, *)`。
